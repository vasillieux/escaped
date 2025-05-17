import pika
import threading
import time
import logging
import os
import httpx
import json
import math

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

RABBITMQ_HOST=os.environ['RABBITMQ_HOST']
RABBITMQ_PORT = port=os.environ['RABBITMQ_PORT']
RABBITMQ_USER = username=os.environ['RABBITMQ_USER']
RABBITMQ_PASSWORD = password=os.environ['RABBITMQ_PASSWORD']
RECONNECT_DELAY = 5 # seconds

PROXIES = [None]
N = len(PROXIES)

class Worker:
    def __init__(self, thread_id, consume_from, publish_to, proxy=None):
        self.thread_id = thread_id
        self.queue_name = consume_from
        self.output_queue_name = publish_to
        self.proxy = proxy
        self.connection = None
        self.channel = None
        self.should_reconnect = False
        self._logger = logging.LoggerAdapter(logging.getLogger(), {'worker_id': self.thread_id})

    def connect(self):
        self._logger.info("Connecting to RabbitMQ...")
        parameters = pika.ConnectionParameters(host=RABBITMQ_HOST,
                                               port=RABBITMQ_PORT,
                                               credentials=pika.PlainCredentials(username=RABBITMQ_USER,
                                                                                 password=RABBITMQ_PASSWORD))
        return pika.SelectConnection(
            parameters=parameters,
            on_open_callback=self.on_connection_open,
            on_open_error_callback=self.on_connection_open_error,
            on_close_callback=self.on_connection_closed
        )

    def on_connection_open(self, connection):
        self._logger.info("Connection opened.")
        self.connection = connection
        self.connection.channel(on_open_callback=self.on_channel_open)

    def on_connection_open_error(self, connection, error):
        self._logger.error(f"Connection open failed: {error}")
        self.reconnect()

    def on_connection_closed(self, connection, reason):
        self._logger.warning(f"Connection closed: {reason}")
        self.reconnect()

    def on_channel_open(self, channel):
        self._logger.info("Channel opened.")
        self.channel = channel
        self.channel.queue_declare(queue=self.queue_name, durable=True)
        self.channel.queue_declare(queue=self.output_queue_name, durable=True)
        self.channel.basic_qos(prefetch_count=1)
        self.channel.basic_consume(queue=self.queue_name, on_message_callback=self.on_message)
        self._logger.info(f"Waiting for messages on '{self.queue_name}'...")

    def on_message(self, ch, method, properties, body):
        self._logger.info(f"Received message: {body.decode()}")
        try:
            self.process_task(body.decode(), properties)
            time.sleep(6)
            ch.basic_ack(delivery_tag=method.delivery_tag)
            self._logger.info("Message processed and acknowledged.")
        except Exception as e:
            self._logger.error(f"Error processing message: {e}", exc_info=True)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def publish_message(self, exchange, routing_key, body, priority=0):
        if self.channel is None or self.channel.is_closing or self.channel.is_closed:
            self._logger.warning("Cannot publish: channel is not open.")
            return

        try:
            self.channel.basic_publish(
                exchange=exchange,
                routing_key=routing_key,
                body=body,
                properties=pika.BasicProperties(
                    delivery_mode=2,  # make message persistent
                    priority=priority
                )
            )
            self._logger.info(f"Published message to '{routing_key}': {body}")
        except Exception as e:
            self._logger.error(f"Failed to publish message: {e}", exc_info=True)


    def process_task(self, task_data, properties):
        MAX_MORE_PAGES = 3
        self._logger.info(f"Processing task: {task_data}")
        with httpx.Client(proxy=self.proxy) as client:
            message = json.loads(task_data)
            query, page = message['query'], message.get('page') or 1
            
            response = client.get(f'https://api.github.com/search/repositories?q={query}&{page=}&per_page=100').json()

        for item in response['items']:
            self.publish_message(exchange='',
                                 routing_key=self.output_queue_name,
                                 body=json.dumps({
                                    'html_url':  item.get('html_url') # TODO: Send ALL desired data to analyzer
                                 }))

    

        if properties.priority == 0:
            total_count = response['total_count']
            total_pages = math.ceil(total_count / 100)
            for p in range( min(total_pages, MAX_MORE_PAGES) - 1 ):
                self.publish_message(exchange='',
                                    routing_key=self.queue_name,
                                    priority=1, # Higher, in order to completely handle first crawl request before handling second.
                                    body=json.dumps({
                                        'query': query,
                                        'page': page + p
                                    }))

    def reconnect(self):
        self._logger.info(f"Reconnecting in {RECONNECT_DELAY} seconds...")
        try:
            self.connection.ioloop.stop()
        except Exception as e:
            self._logger.warning(f"Error stopping IOLoop: {e}")
        time.sleep(RECONNECT_DELAY)
        self.run()

    def run(self):
        while True:
            try:
                self._logger.info("Initializing connection...")
                self.connection = self.connect()
                self.connection.ioloop.start()
                break
            except Exception as e:
                self._logger.error(f"Connection failed: {e}", exc_info=True)
                self._logger.info(f"Retrying in {RECONNECT_DELAY} seconds...")
                time.sleep(RECONNECT_DELAY)


def start_worker(thread_id, consume_from, publish_to, proxy):
    worker = Worker(thread_id, consume_from, publish_to, proxy)
    worker.run()

if __name__ == '__main__':
    threads = []
    for i in range(N):
        t = threading.Thread(target=start_worker, args=(i, 'for_github_crawlers', 'for_github_analyzers', PROXIES[i]))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()