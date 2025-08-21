import redis
from rq import Queue
import os
import time 
import random

from escaped.config import (
    REDIS_HOST, REDIS_PORT,
    REDIS_DB_CRAWLER, CRAWLER_QUEUE_NAME,
    REDIS_DB_ANALYZER, ANALYZER_QUEUE_NAME, 
    REDIS_DB_SEMAPHORE, GLOBAL_MAX_CONCURRENT_PIPELINES, ACTIVE_PIPELINES_COUNTER_KEY
)

SUBMITTER_BATCH_SIZE = 20
SUBMITTER_CHECK_INTERVAL_SECONDS = 30
SUBMITTER_TARGET_ANALYZER_Q_BUFFER = GLOBAL_MAX_CONCURRENT_PIPELINES * 2 # allow analyzer Q to build up a bit more

def get_active_pipelines_count(redis_conn):
    """quick helper to see how many analysis pipelines are running"""
    # if the counter key isn't in redis yet, set it to 0
    if not redis_conn.exists(ACTIVE_PIPELINES_COUNTER_KEY):
        redis_conn.set(ACTIVE_PIPELINES_COUNTER_KEY, 0)
    return int(redis_conn.get(ACTIVE_PIPELINES_COUNTER_KEY) or 0) 


def submit_org_list_to_crawler_limited(org_list_file="web3_orgs.txt"):
    """
    reads org names from a file, and sends them to the crawler queue
    but it does it gently, checking if the system is too busy first.
    """
    print(f"\n--- sending orgs from {org_list_file} to crawler (gently) ---")
    # connect to the crawler's queue
    redis_crawler_q_conn = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB_CRAWLER)
    crawler_q = Queue(CRAWLER_QUEUE_NAME, connection=redis_crawler_q_conn)

    # also need to check the global pipeline counter
    redis_pipeline_counter_conn = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB_SEMAPHORE)

    if not os.path.exists(org_list_file):
        print(f"uh oh, can't find '{org_list_file}'. make sure it's there or make a new one.")
        return

    with open(org_list_file, "r") as f:
        # grab all org names, skip empty lines and comments (#)
        all_organizations = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    
    if not all_organizations:
        print(f"'{org_list_file}' is empty or just comments. nothing to do here.")
        return

    total_orgs_to_submit = len(all_organizations)
    num_batches_enqueued = 0
    num_orgs_processed_by_submitter = 0

    # process orgs in chunks (batches)
    for i in range(0, total_orgs_to_submit, SUBMITTER_BATCH_SIZE):
        current_batch_of_orgs = all_organizations[i:i + SUBMITTER_BATCH_SIZE]
        if not current_batch_of_orgs: 
            continue # should not happen if loop logic is right

        # --- this is the "be nice" part: check system load before submitting more ---
        while True:
            active_pipelines = get_active_pipelines_count(redis_pipeline_counter_conn)
            
            # check length of queues this submitter feeds into
            len_crawler_q = crawler_q.count # crawler's own queue
            # and the one after that (analyzer queue)
            redis_analyzer_q_conn_for_check = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB_ANALYZER)
            len_analyzer_q = Queue(ANALYZER_QUEUE_NAME, connection=redis_analyzer_q_conn_for_check).count
            
            combined_q_len = len_crawler_q + len_analyzer_q

            # heuristic: if active pipelines are well below max, AND queues aren't crazy long, go ahead.
            # '+5' gives a bit of headroom over the strict worker limit for submissions.
            # SUBMITTER_TARGET_ANALYZER_Q_BUFFER * 2 is also a soft target for combined queue length.
            if active_pipelines < (GLOBAL_MAX_CONCURRENT_PIPELINES + 5) and \
               combined_q_len < (SUBMITTER_TARGET_ANALYZER_Q_BUFFER * 2) :
                print(f"submitter: system looks ok (active: {active_pipelines}, queues total: {combined_q_len}). sending batch.")
                break # ok to submit this batch
            else:
                # system is busy or queues are full, wait a bit
                wait_duration = SUBMITTER_CHECK_INTERVAL_SECONDS * 0.5 + random.uniform(0, 5) # shorter wait for submitter
                print(f"submitter: system busy (active: {active_pipelines}, queues: {combined_q_len}). waiting {wait_duration:.1f}s...")
                time.sleep(wait_duration)
        
        # enqueue this batch of orgs for the crawler to process
        # the crawler job `discover_repos_from_org_list_job` is designed to take a list of orgs.
        print(f"sending batch of {len(current_batch_of_orgs)} orgs to crawler queue...")
        job = crawler_q.enqueue('escaped.workers.crawler.discover_repos_from_org_list_job', 
                                current_batch_of_orgs, job_timeout='1h') # crawler job gets 1hr
        num_batches_enqueued += 1
        num_orgs_processed_by_submitter += len(current_batch_of_orgs)
        print(f"  batch job id: {job.id}. (processed {num_orgs_processed_by_submitter}/{total_orgs_to_submit} orgs by submitter)")
        
        time.sleep(random.uniform(0.5, 1.5)) # small pause between sending batches

    print(f"all done submitting {total_orgs_to_submit} orgs (in {num_batches_enqueued} batches) to crawler.")


def submit_gh_search_to_crawler_limited(search_query, gh_results_limit=50):
    """
    sends a github search query to the crawler.
    it waits if the system is busy before sending.
    """
    print(f"\n--- sending github search '{search_query}' to crawler (gently) ---")
    if not search_query: 
        print("need a search query, buddy.")
        return

    redis_crawler_q_conn = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB_CRAWLER)
    crawler_q = Queue(CRAWLER_QUEUE_NAME, connection=redis_crawler_q_conn)
    redis_pipeline_counter_conn = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB_SEMAPHORE)

    # wait for system capacity before sending this one search job
    while True:
        active_pipelines = get_active_pipelines_count(redis_pipeline_counter_conn)
        len_crawler_q = crawler_q.count
        redis_analyzer_q_conn_for_check = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB_ANALYZER)
        len_analyzer_q = Queue(ANALYZER_QUEUE_NAME, connection=redis_analyzer_q_conn_for_check).count
        combined_q_len = len_crawler_q + len_analyzer_q

        if active_pipelines < (GLOBAL_MAX_CONCURRENT_PIPELINES + 5) and \
           combined_q_len < (SUBMITTER_TARGET_ANALYZER_Q_BUFFER * 2):
            print(f"submitter: system looks ok for search job. sending it.")
            break
        else:
            wait_duration = SUBMITTER_CHECK_INTERVAL_SECONDS * 0.5 + random.uniform(0, 5)
            print(f"submitter: system busy for search job. waiting {wait_duration:.1f}s...")
            time.sleep(wait_duration)
    
    print(f"sending github search query to crawler: '{search_query}' (gh limit: {gh_results_limit})")
    job = crawler_q.enqueue('escaped.workers.crawler.discover_repos_from_gh_search_job', 
                            search_query, gh_results_limit, job_timeout='1h')
    print(f"  github search job id: {job.id}")



def submit_direct_repo_list_to_analyzer_limited(repo_list_file="direct_repos_to_analyze.txt"):
    """
    reads 'org/repo' lines from a file and sends them straight to the analyzer queue.
    still checks system load before sending each small batch.
    """
    print(f"\n--- sending direct repos from {repo_list_file} to analyzer (gently) ---")
    # this sends jobs directly to the analyzer's queue
    redis_analyzer_q_conn = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB_ANALYZER)
    analyzer_q = Queue(ANALYZER_QUEUE_NAME, connection=redis_analyzer_q_conn)
    redis_pipeline_counter_conn = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB_SEMAPHORE)

    if not os.path.exists(repo_list_file):
        print(f"can't find '{repo_list_file}'. maybe create one with 'org/repo' on each line?")
        # example: create a dummy file
        with open(repo_list_file, "w") as f: f.write("trufflesecurity/trufflehog\n")
        return

    with open(repo_list_file, "r") as f:
        all_repo_full_names = [line.strip() for line in f if line.strip() and not line.startswith("#") and '/' in line]

    if not all_repo_full_names:
        print(f"no valid 'org/repo' lines in '{repo_list_file}'.")
        return

    total_repos_to_submit = len(all_repo_full_names)
    num_repos_enqueued = 0

    # process in batches, but check capacity for *each individual repo* in this direct mode
    # because each one becomes an analyzer job immediately.
    for i in range(0, total_repos_to_submit, SUBMITTER_BATCH_SIZE):
        batch_of_repos = all_repo_full_names[i:i + SUBMITTER_BATCH_SIZE]
        if not batch_of_repos: 
            continue

        for full_repo_name in batch_of_repos:
            # check system load before sending this single repo job
            while True:
                active_pipelines = get_active_pipelines_count(redis_pipeline_counter_conn)
                # here, we care mostly about the analyzer queue length since we're feeding it directly
                len_analyzer_q = analyzer_q.count 

                # be a bit more conservative here: only submit if active pipelines are clearly below max
                # OR if active are at max, but analyzer queue is getting very short.
                if active_pipelines < GLOBAL_MAX_CONCURRENT_PIPELINES or \
                    (active_pipelines == GLOBAL_MAX_CONCURRENT_PIPELINES and len_analyzer_q < SUBMITTER_TARGET_ANALYZER_Q_BUFFER / 2): # more aggressive feeding if at max
                    print(f"submitter: system ok (active: {active_pipelines}, analyzerQ: {len_analyzer_q}). sending direct: {full_repo_name}")
                    break
                else:
                    # shorter wait for individual items in a direct batch
                    wait_duration = SUBMITTER_CHECK_INTERVAL_SECONDS * 0.25 + random.uniform(0, 2) 
                    print(f"submitter: system busy for direct send (active: {active_pipelines}, analyzerQ: {len_analyzer_q}). waiting {wait_duration:.1f}s for {full_repo_name}...")
                    time.sleep(wait_duration)
            
            try:
                org_name, repo_name = full_repo_name.split('/', 1)
                # send job directly to analyzer
                job = analyzer_q.enqueue( 
                    'escaped.workers.analyzer.analyze_repository_job',
                    org_name, repo_name, job_timeout='3h' # analyzer job gets longer timeout
                )
                num_repos_enqueued += 1
                print(f"  sent direct analysis for: {org_name}/{repo_name}. id: {job.id}. ({num_repos_enqueued}/{total_repos_to_submit})")
            except ValueError:
                print(f"  oops, bad format for direct repo: '{full_repo_name}'. skipping it.")
                continue
            time.sleep(random.uniform(0.05, 0.2)) # very tiny pause between each direct send

    print(f"all done submitting {num_repos_enqueued} direct repos to analyzer.")


def main():
    # make sure dummy input files exist 
    if not os.path.exists("web3_orgs.txt"):
        with open("web3_orgs.txt", "w") as f: f.write("# add github org names, one per line\ntrufflesecurity\n")
    if not os.path.exists("direct_repos_to_analyze.txt"):
        with open("direct_repos_to_analyze.txt", "w") as f: f.write("# add 'org/repo', one per line\ntrufflesecurity/trufflehog\n")


    # running options bellow 

    # 1. directly to start analyzer
    # submit_org_list_to_crawler_limited(org_list_file="web3_orgs.txt")

    # 2. send a github search query to the crawler
    # my_search_query = 'language:Solidity "Ownable.sol" stars:>10'
    # submit_gh_search_to_crawler_limited(search_query=my_search_query, gh_results_limit=20) # small limit for testing

    # 3. send a list of specific repos straight to the analyzer
    submit_direct_repo_list_to_analyzer_limited(repo_list_file="direct_repos_to_analyze.txt")
    
    print(f"\n--- submitter script finished (or is still gently submitting) ---")
    print("check worker logs and 'rq info' in another terminal to see what's happening.")
    print(f"remember, global max concurrent pipelines is set to: {GLOBAL_MAX_CONCURRENT_PIPELINES}")

if __name__ == "__main__":
    main()