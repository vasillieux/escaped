## Gitsens 

What is? Semantic scanner for thousands of github repository 

Parsing repositories with filters for *blobs*:
    - Codebase languages ( contains `.html, .py, .rs, .json, .sol` )
    - Codebase configuration files (`docker-compose.yaml`, `.env`, `.k8s`, '.js', `.py`)
    - Generic binary files  
        - Compiled .exe with patched secrets 
        - `.pyc` and others language-specific precompiled/cache  

Simply, for each of the blob filetype we have to run different regexp parsers

## Installation (No DOCKER) 

### Prerequirements 
- Trufflehog 
- GH (Github-CLI)

Then run with python 3.12 
- `pip install -r requirements.txt`

## Using GH 

### Solidity projects with "hardhat"
- `gh search repos --limit 500 --json fullName --jq '.[].fullName' 'hardhat language:Solidity' > hardhat_solidity_repos.txt`

### Python projects using web3.py
- `gh search repos --limit 500 --json fullName --jq '.[].fullName' 'language:Python web3.py' > python_web3py_repos.txt`

### Files named .env that might contain PRIVATE_KEY
- `gh search code --limit 500 --json repository.fullName,path --jq '.[] | .repository.fullName + "/" + .path' 'filename:.env PRIVATE_KEY' > potential_dotenv_leaks.txt`


### Running 

Start Crawler Worker(s):
- `rq worker -c config web3_crawler_queue --url redis://localhost:6379/0`

Start Analyzer Worker(s):
- `rq worker -c config web3_analyzer_queue --url redis://localhost:6379/1`

Submit Initial Jobs:
- `python gitsens/submit_jobs.py`


## Installation (Docker)

Simply make sure you have docker engine, docker-compose
But you should probably login in your gh via github-cli. Docker-compose will mount this folder from your local machine 
```yaml
volumes:
  - ~/.config/gh:/root/.config/gh:ro
```

## Running 
- `docker-compose up --build`

Check logs 

`docker-compose logs -f crawler_worker`
`docker-compose logs -f analyzer_worker`


## Constraints 
- GH api limit 50000 requrests/hour

## How to win 

It's a secret. Stay tuned.