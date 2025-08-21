import os
import shutil
import json
import time
import subprocess
import random
import redis 
from rq import Queue, get_current_job

from escaped.config import (
    GIT_CLONE_PATH, RESTORED_FILES_PATH, DANGLING_BLOBS_PATH,
    TRUFFLEHOG_RESULTS_PATH, CUSTOM_REGEX_RESULTS_PATH,
    REPO_CLONE_TIMEOUT, TRUFFLEHOG_TIMEOUT,
    GIT_HTTP_PROXY, GIT_HTTPS_PROXY, GIT_PROXY_COMMAND,
    MAX_CLONE_ATTEMPTS, CLONE_RETRY_DELAY_SECONDS,
    REDIS_HOST, REDIS_PORT, REDIS_DB_ANALYZER, ANALYZER_QUEUE_NAME, 
    REDIS_DB_SEMAPHORE, GLOBAL_MAX_CONCURRENT_PIPELINES, ACTIVE_PIPELINES_COUNTER_KEY,
    ANALYZER_REQUEUE_DELAY_SECONDS, 

    SCAN_COMMIT_DEPTH, MAX_FILE_SIZE_TO_SCAN_BYTES, DENYLIST_EXTENSIONS,
    REDIS_DB_CACHE, PROCESSED_REPOS_SET_KEY, PROCESSED_REPOS_CACHE_TTL_SECONDS
)
from escaped.utils import (
    run_command, ALL_HEURISTICS
)

def clone_repo_with_retries(org_name, repo_name):
    """
    tries to clone a repo. uses proxies if set. retries a few times if it fails.
    returns the path where it cloned, or None if it totally failed.
    """
    repo_url = f"https://github.com/{org_name}/{repo_name}.git"
    # make folder names safe for the OS
    safe_org_name = "".join(c if c.isalnum() else "_" for c in org_name)
    safe_repo_name = "".join(c if c.isalnum() else "_" for c in repo_name)
    
    target_path_base = os.path.join(GIT_CLONE_PATH, safe_org_name)
    cloned_repo_path = os.path.join(target_path_base, safe_repo_name) # more descriptive

    # setup environment for git if proxies are needed
    git_env = os.environ.copy() # start with current environment
    proxy_configured = False
    # TODO: Add proxies support
    if GIT_HTTP_PROXY:
        git_env["http_proxy"] = GIT_HTTP_PROXY
        git_env["HTTP_PROXY"] = GIT_HTTP_PROXY # some tools like uppercase
        proxy_configured = True
    if GIT_HTTPS_PROXY:
        git_env["https_proxy"] = GIT_HTTPS_PROXY
        git_env["HTTPS_PROXY"] = GIT_HTTPS_PROXY
        proxy_configured = True
    if GIT_PROXY_COMMAND:
        git_env["GIT_PROXY_COMMAND"] = GIT_PROXY_COMMAND # for things like SOCKS
        proxy_configured = True
        print(f"[analyzer] using GIT_PROXY_COMMAND: {GIT_PROXY_COMMAND}. make sure git knows how to use it.")

    if proxy_configured:
        print(f"[analyzer] trying to clone {repo_url} with proxy settings.")

    for attempt in range(1, MAX_CLONE_ATTEMPTS + 1):
        print(f"[analyzer] cloning {repo_url} to {cloned_repo_path} (attempt {attempt}/{MAX_CLONE_ATTEMPTS})...")
        
        if os.path.exists(cloned_repo_path):
            # print(f"[analyzer] repo {cloned_repo_path} already exists. removing it for a fresh clone.") # a bit noisy
            try:
                shutil.rmtree(cloned_repo_path)
            except OSError as e:
                print(f"[analyzer] oops, couldn't remove old repo folder {cloned_repo_path}: {e}. trying anyway.")
        
        os.makedirs(target_path_base, exist_ok=True) # make sure the org's folder exists
        
        # --filter=blob:none tells git to not download actual file contents initially,
        # thanks @Mira for that finding
        # which is faster if we're mostly interested in history and metadata.
        # trufflehog and our methods will fetch blobs as needed.
        clone_cmd = ["git", "clone", "--filter=blob:none", "--progress", repo_url, cloned_repo_path]
        
        # use our run_command helper, passing the special environment if proxies are set
        # how to pass env in run_command? probably via context like in go?
        result = run_command(clone_cmd, timeout=REPO_CLONE_TIMEOUT, check=False)

        if result and hasattr(result, 'returncode') and result.returncode == 0:
            print(f"[analyzer] cool, cloned {repo_url} to {cloned_repo_path}")
            return cloned_repo_path # success!
        
        # if it failed...
        failure_reason = "unknown error"
        if isinstance(result, subprocess.TimeoutExpired): 
            failure_reason = "timed out"
        elif result and hasattr(result, 'returncode'):
            failure_reason = f"failed with code {result.returncode}"
            if result.stderr: failure_reason += f". git stderr: {result.stderr.strip()}"
        
        print(f"[analyzer] clone attempt {attempt} for {repo_url} {failure_reason}.")

        if attempt < MAX_CLONE_ATTEMPTS:
            # wait a bit before trying again, with some randomness (jitter)
            base_delay = CLONE_RETRY_DELAY_SECONDS * (2 ** (attempt - 1)) # exponential backoff
            jitter = random.uniform(0, CLONE_RETRY_DELAY_SECONDS * 0.25)
            actual_delay = base_delay + jitter
            print(f"[analyzer] waiting {actual_delay:.2f}s before retrying clone...")
            time.sleep(actual_delay)
        else:
            print(f"[analyzer] gave up cloning {repo_url} after {MAX_CLONE_ATTEMPTS} tries.")
            if os.path.exists(cloned_repo_path): # clean up if partial clone happened
                shutil.rmtree(cloned_repo_path, ignore_errors=True)
            return None # all attempts failed
    return None # should not be reached if loop logic is correct

def _get_safe_output_subdir(base_path, org_name, repo_name): 
    """makes a safe directory name like analysis_output/org_name/repo_name"""
    safe_org = "".join(c if c.isalnum() else "_" for c in org_name)
    safe_repo = "".join(c if c.isalnum() else "_" for c in repo_name)
    output_dir = os.path.join(base_path, safe_org, safe_repo)
    os.makedirs(output_dir, exist_ok=True) # fine if it's already there
    return output_dir

def restore_deleted_files_in_repo(cloned_repo_path, org_name, repo_name): 
    """
    goes through git history, finds files that were deleted,
    and saves their content just before they got deleted.
    like that blog post mentioned!
    """
    print(f"[analyzer] looking for deleted files in {cloned_repo_path}...")

    output_base_dir = _get_safe_output_subdir(RESTORED_FILES_PATH, org_name, repo_name)
    log_file = os.path.join(output_base_dir, "_deleted_files_log.txt") # just for us to see what happened

    if SCAN_COMMIT_DEPTH > 0:
        print(f"[analyzer] optimization: scanning only the last {SCAN_COMMIT_DEPTH} commits for deleted files.")
        rev_list_cmd = ["git", "rev-list", f"--max-count={SCAN_COMMIT_DEPTH}", "HEAD"]
    else:
        print(f"[analyzer] deep scan: scanning ALL commits for deleted files.")
        rev_list_cmd = ["git", "rev-list", "--all"]


    commits_result = run_command(rev_list_cmd, cwd=cloned_repo_path)
    if not (commits_result and hasattr(commits_result, 'returncode') and commits_result.returncode == 0 and commits_result.stdout):
        print(f"[analyzer] couldn't get commit list for {cloned_repo_path}. skipping deleted file restore.")
        return output_base_dir 

    all_commit_shas = commits_result.stdout.strip().split('\n')
    # keep track so we don't save the same deleted file version multiple times
    # (e.g. if it was deleted in a branch that got merged weirdly)
    already_restored_versions = {} 

    for commit_sha in all_commit_shas:
        if not commit_sha: continue # skip empty lines if any

        # find the parent(s) of this commit
        parent_commit_cmd = ["git", "log", "--pretty=%P", "-n", "1", commit_sha]
        parent_result = run_command(parent_commit_cmd, cwd=cloned_repo_path)
        if not (parent_result and hasattr(parent_result, 'returncode') and parent_result.returncode == 0 and parent_result.stdout.strip()):
            continue # probably the first commit, or some error

        parent_shas = parent_result.stdout.strip().split()
        if not parent_shas: continue

        for parent_sha in parent_shas: # handle merge commits (multiple parents)
            # see what changed between parent and current commit
            diff_cmd = ["git", "diff", "--name-status", parent_sha, commit_sha]
            diff_result = run_command(diff_cmd, cwd=cloned_repo_path)
            if not (diff_result and hasattr(diff_result, 'returncode') and diff_result.returncode == 0 and diff_result.stdout):
                continue

            for diff_line in diff_result.stdout.strip().split('\n'):
                if not diff_line: continue
                parts = diff_line.split('\t')
                if len(parts) < 2: continue
                
                status_char, file_path_original = parts[0], parts[1]
                # if it was renamed (R100 old new) or copied (C100 old new), we care about the 'old' for deletion context
                if status_char.startswith("R") and len(parts) == 3: file_path_original = parts[1] 
                elif status_char.startswith("C") and len(parts) == 3: file_path_original = parts[1]

                if status_char.startswith("D"): # D means deleted!
                    version_key = f"{parent_sha}:{file_path_original}" # unique key for this file version
                    if version_key in already_restored_versions:
                        continue # got this one already
                    already_restored_versions[version_key] = True

                    # make a safe filename for saving
                    safe_filename = file_path_original.replace('/', '_').replace('\\', '_')
                    output_filepath = os.path.join(output_base_dir, f"commit_{commit_sha}_parent_{parent_sha}_deleted_{safe_filename}")

                    with open(log_file, "a", encoding='utf-8') as lf:
                        lf.write(f"deleted: {file_path_original} (in commit {commit_sha} from parent {parent_sha}), saving to {output_filepath}\n")

                    # get the content of the file AS IT WAS IN THE PARENT COMMIT (before deletion)
                    show_cmd = ["git", "show", f"{parent_sha}:{file_path_original}"]
                    # we want raw bytes because it could be anything
                    content_result = run_command(show_cmd, cwd=cloned_repo_path, capture_output=True, text=False) 

                    if content_result and hasattr(content_result, 'returncode') and content_result.returncode == 0 and content_result.stdout:
                        try:
                            with open(output_filepath, "wb") as restored_f: # write as binary
                                restored_f.write(content_result.stdout)
                        except Exception as e_write:
                            with open(log_file, "a", encoding='utf-8') as lf: lf.write(f"  ERROR writing {output_filepath}: {e_write}\n")
                    elif content_result and hasattr(content_result, 'returncode'): # git show failed
                        with open(log_file, "a", encoding='utf-8') as lf: lf.write(f"  ERROR 'git show' for {file_path_original}. RC: {content_result.returncode}. Stderr: {content_result.stderr.decode(errors='ignore') if content_result.stderr else 'N/A'}\n")
    return output_base_dir


def extract_dangling_blobs_in_repo(cloned_repo_path, org_name, repo_name): 
    """
    finds git 'blobs' (file contents) that aren't part of any commit history anymore
    but might still be lying around in .git/objects.
    """
    print(f"[analyzer] looking for dangling blobs in {cloned_repo_path}...")
    # (Pasting the full refactored function for this one)
    output_base_dir = _get_safe_output_subdir(DANGLING_BLOBS_PATH, org_name, repo_name)
    log_file = os.path.join(output_base_dir, "_dangling_blobs_log.txt")

    # important: unpack .pack files first! dangling objects might be hiding in there.
    # this can take a while and use disk space.
    print(f"[analyzer] unpacking .pack files in {cloned_repo_path} (this might take a moment)...")
    find_packs_cmd = ["find", ".git/objects/pack", "-name", "*.pack", "-type", "f"] # only files
    packs_result = run_command(find_packs_cmd, cwd=cloned_repo_path)
    if packs_result and hasattr(packs_result, 'returncode') and packs_result.returncode == 0 and packs_result.stdout:
        for pack_file_path_relative in packs_result.stdout.strip().split('\n'):
            if not pack_file_path_relative: continue
            # use sh -c for the redirection, quote the path for safety
            unpack_cmd_str = f"git unpack-objects -r < \"{pack_file_path_relative.strip()}\""
            # no need to capture output here, just let it run
            run_command(["sh", "-c", unpack_cmd_str], cwd=cloned_repo_path, capture_output=False) 
    else:
        print(f"[analyzer] no .pack files found or error listing them in {cloned_repo_path}.")

    # now look for dangling stuff
    fsck_cmd = ["git", "fsck", "--full", "--unreachable", "--dangling", "--no-reflogs"]
    fsck_result = run_command(fsck_cmd, cwd=cloned_repo_path)
    if not (fsck_result and hasattr(fsck_result, 'returncode') and fsck_result.returncode == 0 and fsck_result.stdout):
        print(f"[analyzer] 'git fsck' didn't run right or found nothing for {cloned_repo_path}.")
        return output_base_dir

    blob_shas_found = []
    for line in fsck_result.stdout.strip().split('\n'):
        if "unreachable blob" in line: # this is what we want
            parts = line.split()
            if len(parts) >= 3:
                blob_shas_found.append(parts[2]) # the third part is the SHA hash

    if not blob_shas_found:
        print(f"[analyzer] no dangling blobs found by fsck in {cloned_repo_path}.")
        return output_base_dir
        
    print(f"[analyzer] found {len(blob_shas_found)} dangling blob(s). trying to save their content...")
    for blob_sha in blob_shas_found:
        output_filepath = os.path.join(output_base_dir, f"dangling_{blob_sha}.blob")
        # 'git cat-file -p <sha>' prints the content of the blob
        cat_file_cmd = ["git", "cat-file", "-p", blob_sha]
        content_result = run_command(cat_file_cmd, cwd=cloned_repo_path, capture_output=True, text=False) # raw bytes

        if content_result and hasattr(content_result, 'returncode') and content_result.returncode == 0 and content_result.stdout:
            try:
                with open(output_filepath, "wb") as f_blob:
                    f_blob.write(content_result.stdout)
                with open(log_file, "a", encoding='utf-8') as lf: lf.write(f"saved dangling blob: {blob_sha} to {output_filepath}\n")
            except Exception as e_write_blob:
                with open(log_file, "a", encoding='utf-8') as lf: lf.write(f"  ERROR writing blob {output_filepath}: {e_write_blob}\n")
        elif content_result and hasattr(content_result, 'returncode'):
            with open(log_file, "a", encoding='utf-8') as lf: lf.write(f"  ERROR 'git cat-file' for blob {blob_sha}. RC: {content_result.returncode}. Stderr: {content_result.stderr.decode(errors='ignore') if content_result.stderr else 'N/A'}\n")
    return output_base_dir

def extract_dangling_blobs_in_repo(repo_path, org_name, repo_name): 
    print(f"[Analyzer] Extracting dangling blobs for {repo_path}...")

    output_base_dir = _get_safe_output_subdir(DANGLING_BLOBS_PATH, org_name, repo_name)
    log_file_path = os.path.join(output_base_dir, "_dangling_blobs_log.txt")
    print(f"[Analyzer] Unpacking objects in {repo_path} before fsck...")

    find_packs_cmd = ["find", ".git/objects/pack", "-name", "*.pack"]
    packs_result = run_command(find_packs_cmd, cwd=repo_path)

    if packs_result and packs_result.returncode == 0 and packs_result.stdout:
        for pack_file_rel_path in packs_result.stdout.strip().split('\n'):
            if not pack_file_rel_path: continue
            unpack_cmd = f"git unpack-objects -r < \"{pack_file_rel_path.strip()}\"" 
            run_command(["sh", "-c", unpack_cmd], cwd=repo_path, capture_output=False)

    fsck_cmd = ["git", "fsck", "--full", "--unreachable", "--dangling", "--no-reflogs"]
    fsck_result = run_command(fsck_cmd, cwd=repo_path)

    if not fsck_result or fsck_result.returncode != 0 or not fsck_result.stdout:
        return output_base_dir

    blob_hashes = []
    for line in fsck_result.stdout.strip().split('\n'):
        if "unreachable blob" in line:
            parts = line.split()
            if len(parts) >= 3: blob_hashes.append(parts[2])
    for blob_hash in blob_hashes:
        blob_file_path = os.path.join(output_base_dir, f"{blob_hash}.blob")
        cat_file_cmd = ["git", "cat-file", "-p", blob_hash]
        cat_file_result = run_command(cat_file_cmd, cwd=repo_path, capture_output=True, text=False)
        if cat_file_result and cat_file_result.returncode == 0 and cat_file_result.stdout:
            try:
                with open(blob_file_path, "wb") as bf: bf.write(cat_file_result.stdout)
            except Exception as e:
                with open(log_file_path, "a", encoding='utf-8') as lf: lf.write(f"ERROR writing blob {blob_file_path}: {e}\n")
        elif cat_file_result and hasattr(cat_file_result, 'returncode'):
            with open(log_file_path, "a", encoding='utf-8') as lf: lf.write(f"ERROR git cat-file for blob {blob_hash}. RC: {cat_file_result.returncode}. Stderr: {cat_file_result.stderr.decode(errors='ignore') if cat_file_result.stderr else 'N/A'}\n")
    return output_base_dir


def run_trufflehog(scan_path, org_name, repo_name, scan_type="repo_history"): 
    print(f"[Analyzer] Running TruffleHog on {scan_path} ({scan_type})...")
    safe_org = "".join(c if c.isalnum() else "_" for c in org_name)
    safe_repo = "".join(c if c.isalnum() else "_" for c in repo_name)
    results_file = os.path.join(TRUFFLEHOG_RESULTS_PATH, f"{safe_org}_{safe_repo}_{scan_type}_trufflehog.json")

    # trufflehog filesystem --only-verified --print-avg-detector-time --include-detectors="all" ./ > secrets.txt 
    if scan_type == "repo_history":
        abs_scan_path = os.path.abspath(scan_path)
        file_uri_path = f"file://{abs_scan_path}" 
        trufflehog_cmd = ["trufflehog", "git", file_uri_path, "--json"]

        if SCAN_COMMIT_DEPTH > 0:
            print(f"[analyzer] optimization: telling trufflehog to scan max depth of {SCAN_COMMIT_DEPTH}.")
            trufflehog_cmd.append(f"--max-depth={SCAN_COMMIT_DEPTH}")
        else:
            print(f"[analyzer] deep scan: telling trufflehog to scan full history.")

    else:
        trufflehog_cmd = ["trufflehog", "filesystem", "--only-verified", "--print-avg-detector-time", "--include-detectors=all" , scan_path, "--json"]
    print(f"[Analyzer] Executing: {' '.join(trufflehog_cmd)}")
    try:
        process = subprocess.Popen(trufflehog_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        stdout, stderr = process.communicate(timeout=TRUFFLEHOG_TIMEOUT)
        if stderr: print(f"[Analyzer] TruffleHog stderr for {scan_path} ({scan_type}):\n{stderr.strip()}")
        if process.returncode not in [0, 1]: print(f"[Analyzer] TruffleHog exited with unexpected code {process.returncode} for {scan_path} ({scan_type}).")
        if stdout:
            with open(results_file, "w", encoding='utf-8') as f: f.write(stdout)
            print(f"[Analyzer] TruffleHog results saved to {results_file} (Exit code: {process.returncode})")
        elif process.returncode == 0:
            with open(results_file, "w", encoding='utf-8') as f: f.write("[]")
            print(f"[Analyzer] TruffleHog found no secrets in {scan_path} ({scan_type}).")
    except subprocess.TimeoutExpired: print(f"[Analyzer] TruffleHog timed out for {scan_path} ({scan_type})")
    except Exception as e: print(f"[Analyzer] Error running TruffleHog on {scan_path} ({scan_type}): {e}")


def analyze_content_with_heuristics(file_path_for_logging, file_name_for_ext_check, content_str, org_name, repo_name, source_type): 
    findings = []

    for heuristic in ALL_HEURISTICS:
        apply_heuristic = True
        if "target_extensions" in heuristic:
            if not any(file_name_for_ext_check.endswith(ext) for ext in heuristic["target_extensions"]):
                apply_heuristic = False
        if apply_heuristic:
            try:
                for match in heuristic["regex"].finditer(content_str):
                    findings.append({
                        "organization": org_name, "repository": repo_name,
                        "file_path_original": file_path_for_logging,
                        "source_type": source_type, "heuristic_name": heuristic["name"],
                        "matched_text": match.group(0), "start_offset": match.start(), "end_offset": match.end(),
                        "severity": heuristic["severity"], "type": heuristic.get("type", "N/A")
                    })
            except Exception: pass 
    return findings

def run_custom_analyzer_on_path(base_scan_path, org_name, repo_name, source_type_label): # Copied
    print(f"[Analyzer] Running custom regex (ALL HEURISTICS) on {base_scan_path} for {source_type_label}...")
    all_findings = []
    safe_org = "".join(c if c.isalnum() else "_" for c in org_name)
    safe_repo = "".join(c if c.isalnum() else "_" for c in repo_name)

    custom_log_file = os.path.join(CUSTOM_REGEX_RESULTS_PATH, f"{safe_org}_{safe_repo}_{source_type_label}_all_heuristics_findings.json")

    # NOTE sometimes too long, somehow should be limited
    for root, _, files in os.walk(base_scan_path):
        for file_name in files:
            file_path_abs = os.path.join(root, file_name)

            try:
                _, file_extension = os.path.splitext(file_name)
                if file_extension.lower() in DENYLIST_EXTENSIONS:
                    # print(f"[analyzer] skipping denylisted extension: {file_name}") # too noisy 
                    continue
                
                if MAX_FILE_SIZE_TO_SCAN_BYTES > 0:
                    file_size = os.path.getsize(file_path_abs)
                    if file_size > MAX_FILE_SIZE_TO_SCAN_BYTES:
                        print(f"[analyzer] skipping large file: {file_name} ({file_size / 1024:.1f} KB)")
                        continue
            except OSError:
                # file might have been removed between os.walk and os.path.getsize
                continue
            
            if source_type_label == "dangling_blob": file_path_for_logging = file_name 
            else:
                try: file_path_for_logging = os.path.relpath(file_path_abs, base_scan_path)
                except ValueError: file_path_for_logging = file_name
            content_str = ""
            try: 
                with open(file_path_abs, "r", encoding="utf-8", errors="ignore") as f: content_str = f.read()
            except Exception:
                try: 
                    with open(file_path_abs, "rb") as f_bin: binary_content = f_bin.read()
                    content_str = binary_content.decode('utf-8', errors='replace')
                except Exception: continue
            if content_str:
                current_findings = analyze_content_with_heuristics(file_path_for_logging, file_name, content_str, org_name, repo_name, source_type_label)
                all_findings.extend(current_findings)

    if all_findings:
        with open(custom_log_file, "w", encoding='utf-8') as f: json.dump(all_findings, f, indent=2)
        print(f"[Analyzer] Custom regex findings for {base_scan_path} ({source_type_label}) saved to {custom_log_file}")
    else: 
        with open(custom_log_file, "w", encoding='utf-8') as f: json.dump([], f, indent=2)


def run_analyzers(*, path=None, org_name=None, repo_name=None, scan_type=None, enable_trufflehog: bool = True, enable_custom_analyzer: bool = False):

    # TODO: multithreading with semaphore is seems to be good
    if enable_trufflehog is True:
        run_trufflehog(path, org_name, repo_name, scan_type=scan_type)
    if enable_custom_analyzer is True:
        run_custom_analyzer_on_path(path, org_name, repo_name, source_type_label=scan_type)


# TODO: add support for multiple analyzers 
# i.e: trufflehog, regexps 
def analyze_repository_job(org_name, repo_name, enable_trufflehog: bool = True, enable_custom_analyzers: bool = False):
    """
    this is the main job an analyzer worker picks up.
    it tries to get a 'slot' to run, clones the repo, does all the scans,
    then cleans up and releases the slot.
    """
    job_start_time = time.time()
    repo_full_name = f"{org_name}/{repo_name}" # For caching

    print(f"[analyzer] hey, new job! for: {org_name}/{repo_name}")

    # connect to redis for the global pipeline counter and for re-adding this job to its own queue if needed
    redis_pipeline_counter_conn = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB_SEMAPHORE)
    redis_analyzer_q_conn = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB_ANALYZER)
    analyzer_queue_self = Queue(ANALYZER_QUEUE_NAME, connection=redis_analyzer_q_conn)

    # --- check if we can run (global pipeline limit) ---
    # make sure the counter exists in redis
    if not redis_pipeline_counter_conn.exists(ACTIVE_PIPELINES_COUNTER_KEY):
        redis_pipeline_counter_conn.set(ACTIVE_PIPELINES_COUNTER_KEY, 0) # initialize it if not there
        
    num_active_pipelines = int(redis_pipeline_counter_conn.get(ACTIVE_PIPELINES_COUNTER_KEY) or 0)

    if num_active_pipelines >= GLOBAL_MAX_CONCURRENT_PIPELINES:
        print(f"[analyzer] too many pipelines running ({num_active_pipelines}/{GLOBAL_MAX_CONCURRENT_PIPELINES}). re-queuing {org_name}/{repo_name} for later.")
        
        current_rq_job = get_current_job(redis_analyzer_q_conn) # get this job's object
        
        delay_for_requeue = ANALYZER_REQUEUE_DELAY_SECONDS + random.uniform(0, 30) # add some randomness
        # get current job's timeout or use a default
        job_timeout = current_rq_job.timeout if current_rq_job and current_rq_job.timeout else '3h' 

        # rq 1.10+ has enqueue_in for delayed jobs
        # retry..
        try: 
            from datetime import timedelta # only import if needed
            analyzer_queue_self.enqueue_in(
                timedelta(seconds=delay_for_requeue), # use timedelta
                'escaped.workers.analyzer.analyze_repository_job',
                org_name, repo_name,
                job_timeout=job_timeout
            )
        except (AttributeError, ImportError): # fallback for older RQ or if timedelta import fails
            # just put it back at the end of the queue. it'll wait its turn.
            analyzer_queue_self.enqueue(
                'escaped.workers.analyzer.analyze_repository_job',
                org_name, repo_name,
                job_timeout=job_timeout
            )
        print(f"[analyzer] re-queued {org_name}/{repo_name} to run in about {delay_for_requeue:.0f}s.")
        return f"re-queued {org_name}/{repo_name}, system busy."

    # --- got a slot! increment the counter ---
    current_pipeline_id = redis_pipeline_counter_conn.incr(ACTIVE_PIPELINES_COUNTER_KEY)
    print(f"[analyzer] grabbed slot #{current_pipeline_id}. active pipelines: {current_pipeline_id}. starting work on {org_name}/{repo_name}")

    local_repo_path = None # path where repo is cloned
    did_analysis_finish_ok = False
    try:
        # --- 1. clone the repo (this has retries built in) ---
        local_repo_path = clone_repo_with_retries(org_name, repo_name)
        
        if not local_repo_path:
            print(f"[analyzer] EPIC FAIL: couldn't clone {org_name}/{repo_name}. stopping analysis for this one.")
            # the 'finally' block will still run to decrement the counter
            return f"clone failed for {org_name}/{repo_name}"

        # --- 2. do all the scanning ---
        print(f"[analyzer] ok, repo cloned to {local_repo_path}. starting scans...")

        # ! NOTE is it makes sense to run trufflehog few times?
        # ! NOTE i guess trufflehog(repo_1 | repo_2 | repo_3) is the same that trufflehog(repo1) | trufflehog(repo2) | trufflehog(repo3)
        
        # A. trufflehog on the whole git history
        # run_trufflehog(local_repo_path, org_name, repo_name, scan_type="local_repo")
        run_analyzers(path=local_repo_path, org_name=org_name, repo_name=repo_name, scan_type="local_repo", enable_trufflehog=enable_trufflehog)

        # B. find deleted files, save them, then scan them
        path_to_restored_files = restore_deleted_files_in_repo(local_repo_path, org_name, repo_name)
        # check if anything was actually restored before scanning an empty folder
        if os.path.exists(path_to_restored_files) and any(f.is_file() for f in os.scandir(path_to_restored_files) if not f.name.startswith('_')):
            print(f"[analyzer] found/restored some deleted files at {path_to_restored_files}, scanning them...")
            run_analyzers(
                path=path_to_restored_files,
                org_name=org_name, 
                repo_name=repo_name, 
                scan_type="restored_files",
                enable_trufflehog=enable_trufflehog,
                enable_custom_analyzer=enable_custom_analyzers,
            )
            # run_trufflehog(path_to_restored_files, org_name, repo_name, scan_type="restored_files")
            # run_custom_analyzer_on_path(path_to_restored_files, org_name, repo_name, source_type_label="restored_file")
        else:
            print(f"[analyzer] no deleted files were restored (or folder is empty) for {org_name}/{repo_name}.")

        # C. find dangling blobs, save them, then scan them
        path_to_dangling_blobs = extract_dangling_blobs_in_repo(local_repo_path, org_name, repo_name)
        if os.path.exists(path_to_dangling_blobs) and any(f.is_file() for f in os.scandir(path_to_dangling_blobs) if not f.name.startswith('_')):
            print(f"[analyzer] found some dangling blobs at {path_to_dangling_blobs}, scanning them...")

            run_analyzers(
                path=path_to_dangling_blobs,
                org_name=org_name, 
                repo_name=repo_name, 
                scan_type="dangling_blobs",
                enable_trufflehog=enable_trufflehog,
                enable_custom_analyzer=enable_custom_analyzers,
            )
            # run_trufflehog(path_to_dangling_blobs, org_name, repo_name, scan_type="dangling_blobs")
            # run_custom_analyzer_on_path(path_to_dangling_blobs, org_name, repo_name, source_type_label="dangling_blob")
        else:
            print(f"[analyzer] no dangling blobs found (or folder is empty) for {org_name}/{repo_name}.")

        # D. scan the current files in the cloned repo (not just history/deleted)
        print(f"[analyzer] scanning current files in the cloned repo at {local_repo_path}...")
        # run_custom_analyzer_on_path(local_repo_path, org_name, repo_name, source_type_label="cloned_repo_current_fs")
        
        did_analysis_finish_ok = True # if we made it here, all main steps were attempted

    except Exception as e_big_job_error:
        print(f"[analyzer] !!! BIG PROBLEM !!! unexpected error during main analysis of {org_name}/{repo_name}: {e_big_job_error}")
        # 'finally' will still run. re-raise so RQ knows this job bombed.
        raise
    finally:
        # --- 3. ALWAYS clean up and release the slot ---
        if local_repo_path and os.path.exists(local_repo_path):
            print(f"[analyzer] cleaning up cloned repo folder: {local_repo_path}")
            shutil.rmtree(local_repo_path, ignore_errors=True) # ignore_errors is safer
        
        # release the pipeline slot by decrementing the counter
        try:
            # decr is atomic. if counter was already 0 (shouldn't happen), it would go negative if not careful,
            # but our incr/decr logic should keep it >= 0.
            new_counter_val = redis_pipeline_counter_conn.decr(ACTIVE_PIPELINES_COUNTER_KEY)
            print(f"[analyzer] released pipeline slot for {org_name}/{repo_name}. active pipelines now: {max(0, new_counter_val)}")
        except Exception as e_redis_cleanup:
            # this is bad, counter might be stuck high. needs monitoring!
            print(f"[analyzer] !!! CRITICAL ERROR !!! failed to release pipeline slot for {org_name}/{repo_name}: {e_redis_cleanup}")

        total_job_time = time.time() - job_start_time
        if did_analysis_finish_ok:
            print(f"[analyzer] YAY! all analysis done for {org_name}/{repo_name}. took {total_job_time:.2f}s.")
            redis_cache_conn = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB_CACHE)
            print(f"[Analyzer] Caching repo as processed: {repo_full_name}")
            
            # SADD returns 1 if the element was added, 0 if it was already there.
            redis_cache_conn.sadd(PROCESSED_REPOS_SET_KEY, repo_full_name)
            cache_key = f"escaped:processed:{repo_full_name}"
            redis_cache_conn.set(cache_key, 1) # dummy val 

            if PROCESSED_REPOS_CACHE_TTL_SECONDS > 0:
                redis_cache_conn.expire(cache_key, PROCESSED_REPOS_CACHE_TTL_SECONDS)
                print(f"[Analyzer] Repo {repo_full_name} will be eligible for rescan in {PROCESSED_REPOS_CACHE_TTL_SECONDS / 3600:.1f} hours.")

            return f"analyzed {org_name}/{repo_name} successfully."
        else:
            print(f"[analyzer] analysis for {org_name}/{repo_name} didn't finish right. took {total_job_time:.2f}s.")
            return f"analysis incomplete/failed for {org_name}/{repo_name}."