"""Connections to Redis, MongoDB and Elasticsearch.
(PostgreSQL has its own file: app/db.py.)"""
from functools import lru_cache

import redis
from elasticsearch import Elasticsearch
from pymongo import MongoClient

from app import config

# Names shared by the loader, the API and the tests
ES_INDEX = "watchlist_names"
MONGO_DB_NAME = "nbi_poc"
TIMELINE_COLLECTION = "status_timeline"


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    # decode_responses=True gives us normal text instead of raw bytes
    return redis.Redis(
        host=config.REDIS_HOST,
        port=config.REDIS_PORT,
        password=config.REDIS_PASSWORD,
        decode_responses=True,
    )


@lru_cache(maxsize=1)
def get_mongo_db():
    client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=5000)
    return client[MONGO_DB_NAME]


@lru_cache(maxsize=1)
def get_es() -> Elasticsearch:
    return Elasticsearch(config.ES_URL, request_timeout=30)