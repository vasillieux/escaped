## escaped

**What is**? Semantic scanner for thousands of (git-) github repositories on leaing sensitive information.

**The flow**:
1. Parsing repositories
2. Checking deleted/history data with filters (**blobs** data)
    - Codebase languages ( contains `.html, .py, .rs, .json, .sol` )
    - Codebase configuration files (`docker-compose.yaml`, `.env`, `.k8s`, '.js', `.py`)
    - Generic binary files 
        - Compiled .exe with patched secrets 
        - `.pyc` and others language-specific precompiled/cache  

3. Simply, for each of the blob filetype we have to run different regexp parsers + trufflehog.

## Installation (No DOCKER) 

### Prerequirements 
- Trufflehog 
- GH (Github-CLI) 
    - You need to setup your GH cli before run the program.

Then run with python 3.12 
- `pip install -r requirements.txt`

## Using GH, to locate the repositories you want to parse. 

### Solidity projects with "hardhat"
- `gh search repos --limit 500 --json fullName --jq '.[].fullName' 'hardhat language:Solidity' > hardhat_solidity_repos.txt`

### Python projects using web3.py
- `gh search repos --limit 500 --json fullName --jq '.[].fullName' 'language:Python web3.py' > python_web3py_repos.txt`

### Files named .env that might contain PRIVATE_KEY
- `gh search code --limit 500 --json repository.fullName,path --jq '.[] | .repository.fullName + "/" + .path' 'filename:.env PRIVATE_KEY' > potential_dotenv_leaks.txt`


Make sure that redis is running 
To start redis:
- `docker-compose up redis`

### Usage (Manual) 

Start Crawler Worker(s):
- `rq worker -c escaped.config escaped_crawler_queue --url redis://localhost:6379/0`

Start Analyzer Worker(s):
- `rq worker -c escaped.config escaped_analyzer_queue --url redis://localhost:6379/1`

Submit Initial Jobs:
- `python escaped/submit_jobs.py`
! Warning. If you're submitting jobs to analyzer directly, specify (populize) file, commonly named `direct_repos_to_analyze.txt`. 
To check the details, look at the `escaped/submit_jobs` implementation.

## Installation (Docker)

Simply make sure you have docker engine, docker-compose
But you should probably login in your gh via github-cli. Docker-compose will mount this folder from your local machine 
```yaml
volumes:
  - ~/.config/gh:/root/.config/gh:ro
```

### Usage (Docker) 
- `docker-compose up --build`

Check logs 

`docker-compose logs -f crawler_worker`
`docker-compose logs -f analyzer_worker`


## Process
After start the analyzer will clone with batch the specified repository in 
`./analysis_output`

And the tree will looks like

```
analysis_output
    ├── cloned_repos 
    ├── custom_regex_findings
    ├── dangling_blobs
    ├── restored_files
    └── trufflehog_findings
```

## Constraints 
- GH api limit 5000 requests/hour
- Limited heuristics
- No post-analysis of output data 
- Only GITHUB (for a while).

## How to win 

It's a secret. Stay tuned.


## Credits 

Thanks trufflehog for the great security and reconnaissance tool!
You can find it at - https://github.com/trufflesecurity/trufflehog