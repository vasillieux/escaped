import pika
from typing import List
import logging
import os

RABBITMQ_HOST=os.environ['RABBITMQ_HOST']
RABBITMQ_PORT = port=os.environ['RABBITMQ_PORT']
RABBITMQ_USER = username=os.environ['RABBITMQ_USER']
RABBITMQ_PASSWORD = password=os.environ['RABBITMQ_PASSWORD']

logger = logging.getLogger(__name__)

class RabbitMQPublisher:
    def __init__(self, queues: List[str]):
        self.queues = queues
        self.connection = None
        self.channel = None

    def connect(self):
        logger.info("Connecting to RabbitMQ...")
        parameters = pika.ConnectionParameters(host=RABBITMQ_HOST,
                                               port=RABBITMQ_PORT,
                                               credentials=pika.PlainCredentials(username=RABBITMQ_USER,
                                                                                 password=RABBITMQ_PASSWORD))
        self.connection = pika.BlockingConnection(parameters)
        self.channel = self.connection.channel()
        for q in self.queues:
            self.channel.queue_declare(queue=q, durable=True)
        logger.info("RabbitMQ connection established.")

    def publish(self, queue_name, message: str):
        if self.channel is None or self.channel.is_closed:
            raise RuntimeError("RabbitMQ channel not available.")
        self.channel.basic_publish(
            exchange='',
            routing_key=queue_name,
            body=message,
            properties=pika.BasicProperties(delivery_mode=2)
        )
        logger.info(f"Published message to {queue_name}: {message}")

    def close(self):
        if self.connection and self.connection.is_open:
            self.connection.close()
            logger.info("RabbitMQ connection closed.")
