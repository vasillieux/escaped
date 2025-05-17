from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import Response
from src.api.publisher import RabbitMQPublisher
from src.api.models import CrawlRequest
import os
import json
import logging

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

publisher = RabbitMQPublisher(queues=[
    'for_github_crawlers'
])


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting FastAPI app with lifespan...")
    publisher.connect()

    yield

    logger.info("Shutting down FastAPI app...")
    publisher.close()

app = FastAPI(lifespan=lifespan)


@app.post('/crawl')
async def submit_crawl(r: CrawlRequest):
    if r.platform == 'github':
        publisher.publish(
            queue_name='for_github_crawlers',
            message=json.dumps({
                'query': r.query
            })
        )
        return Response(content='GitHub seach submitted!', status_code=200)
    return Response('Platform not supported yet', status_code=417)