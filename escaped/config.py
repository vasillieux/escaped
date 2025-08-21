import os

# --- global concurrency stuff ---
# how many full analysis pipelines can run at once (cloning + all analysis)
GLOBAL_MAX_CONCURRENT_PIPELINES = int(os.getenv("GLOBAL_MAX_CONCURRENT_PIPELINES", 10)) 
# redis key for tracking active pipelines
ACTIVE_PIPELINES_COUNTER_KEY = "escaped:active_pipelines"
# how long an analyzer job waits if all pipelines are busy, before trying again
ANALYZER_REQUEUE_DELAY_SECONDS = int(os.getenv("ANALYZER_REQUEUE_DELAY_SECONDS", 120)) 


# --- redis settings for rq ---
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = 6379 # default redis port
REDIS_DB_CRAWLER = 0  # for crawler jobs
REDIS_DB_ANALYZER = 1 # for analyzer jobs
CRAWLER_QUEUE_NAME = "web3_crawler_queue"
ANALYZER_QUEUE_NAME = "web3_analyzer_queue"
# redis db for the global pipeline counter/semaphore
REDIS_DB_SEMAPHORE = int(os.getenv("REDIS_DB_SEMAPHORE", 3)) 

# --- where we dump output files ---
BASE_OUTPUT_DIR = "analysis_output" # main folder for all results
GIT_CLONE_PATH = os.path.join(BASE_OUTPUT_DIR, "cloned_repos") # where repos get cloned temporarily
RESTORED_FILES_PATH = os.path.join(BASE_OUTPUT_DIR, "restored_files") # for files we bring back from git history
DANGLING_BLOBS_PATH = os.path.join(BASE_OUTPUT_DIR, "dangling_blobs") # for orphaned git objects
TRUFFLEHOG_RESULTS_PATH = os.path.join(BASE_OUTPUT_DIR, "trufflehog_findings") # trufflehog's json output
CUSTOM_REGEX_RESULTS_PATH = os.path.join(BASE_OUTPUT_DIR, "custom_regex_findings") # our regex scanner's json output

# --- github api stuff ---
# !TODO use multiply token for more index. (github service)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") 

# --- various limits and timeouts ---
MAX_REPOS_PER_ORG = 200 # safety net for `gh repo list`
# REPO_CLONE_TIMEOUT = 1800 # 30 mins (defined again below, remove one)
TRUFFLEHOG_TIMEOUT = 1800 # 30 mins for trufflehog scan

# -- !NOTE for now proxy's not working --
# --- cloning specific settings (used by analyzer) ---
MAX_CLONE_ATTEMPTS = 3 # how many times to try cloning before giving up
CLONE_RETRY_DELAY_SECONDS = 60 # base delay between clone retries
REPO_CLONE_TIMEOUT = 1800 # 30 mins for a single clone attempt
# http/s proxy for git if you need it
GIT_HTTP_PROXY = os.getenv("GIT_HTTP_PROXY")
GIT_HTTPS_PROXY = os.getenv("GIT_HTTPS_PROXY")
# for more advanced proxying like SOCKS
GIT_PROXY_COMMAND = os.getenv("GIT_PROXY_COMMAND")


# make sure output folders exist
for path in [GIT_CLONE_PATH, RESTORED_FILES_PATH, DANGLING_BLOBS_PATH,
            TRUFFLEHOG_RESULTS_PATH, CUSTOM_REGEX_RESULTS_PATH]:
    os.makedirs(path, exist_ok=True) # exist_ok=True means no error if folder is already there