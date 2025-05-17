from pydantic import BaseModel
from enum import Enum
from typing import List

class Platform(str, Enum):
    GITHUB = "github"
    GITLAB = "gitlab"

class CrawlRequest(BaseModel):
    platform: Platform
    query: str