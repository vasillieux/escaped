## escaped

- [escaped](#escaped)
- [installation (No DOCKER)](#installation-no-docker)
  - [prerequirements](#prerequirements)
- [using GH, to locate the repositories you want to parse.](#using-gh-to-locate-the-repositories-you-want-to-parse)
  - [solidity projects with "hardhat"](#solidity-projects-with-hardhat)
  - [python projects using web3.py](#python-projects-using-web3py)
  - [files named .env that might contain PRIVATE\_KEY](#files-named-env-that-might-contain-private_key)
  - [usage (Manual)](#usage-manual)
- [installation (Docker)](#installation-docker)
  - [usage (Docker)](#usage-docker)
- [process](#process)
- [constraints](#constraints)
- [how git works (learn)](#how-git-works-learn)
- [credits](#credits)

**w-w-what**? Semantic scanner for thousands of (git-) github repositories on leaing sensitive information.

**flow**:
1. Parsing repositories
2. Checking deleted/history data with filters (**blobs** data)
    - Codebase languages ( contains `.html, .py, .rs, .json, .sol` )
    - Codebase configuration files (`docker-compose.yaml`, `.env`, `.k8s`, '.js', `.py`)
    - Generic binary files 
        - Compiled .exe with patched secrets 
        - `.pyc` and others language-specific precompiled/cache  

3. Simply, for each of the blob filetype we have to run different regexp parsers + trufflehog.

## installation (No DOCKER) 

### prerequirements 
- Trufflehog 
- GH (Github-CLI) (https://github.com/cli/cli/blob/trunk/docs/install_linux.md)
    - You need to setup your GH cli before run the program.

Then run with python 3.12 
- `pip install -r requirements.txt`
or 
- `pip install -e .`

## using GH, to locate the repositories you want to parse. 

### solidity projects with "hardhat"
- `gh search repos --limit 500 --json fullName --jq '.[].fullName' 'hardhat language:Solidity' > hardhat_solidity_repos.txt`

### python projects using web3.py
- `gh search repos --limit 500 --json fullName --jq '.[].fullName' 'language:Python web3.py' > python_web3py_repos.txt`

### files named .env that might contain PRIVATE_KEY
- `gh search code --limit 500 --json repository.fullName,path --jq '.[] | .repository.fullName + "/" + .path' 'filename:.env PRIVATE_KEY' > potential_dotenv_leaks.txt`


Make sure that redis is running 
To start redis:
- `docker-compose up redis`

### usage (Manual) 

Start Crawler Worker(s):
- `rq worker -c escaped.config escaped_crawler_queue --url redis://localhost:6379/0`

Start Analyzer Worker(s):
- `rq worker -c escaped.config escaped_analyzer_queue --url redis://localhost:6379/1`

Submit Initial Jobs:
- `python escaped/submit_jobs.py`
! Warning. If you're submitting jobs to analyzer directly, specify (populize) file, commonly named `direct_repos_to_analyze.txt`. 
To check the details, look at the `escaped/submit_jobs` implementation.

## installation (Docker)

Simply make sure you have docker engine, docker-compose
But you should probably login in your gh via github-cli. Docker-compose will mount this folder from your local machine 
```yaml
volumes:
  - ~/.config/gh:/root/.config/gh:ro
```

### usage (Docker) 
- `docker-compose up --build`

Check logs 

`docker-compose logs -f crawler_worker`
`docker-compose logs -f analyzer_worker`


## process
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

## constraints 
- GH api limit 5000 requests/hour
- Limited heuristics
- No post-analysis of output data 
- Only GITHUB (for a while).

## how git works (learn)

basically, each (binary, source) file's content (!) is indexed via blob in git.
next here's something called a tree. intuition may sound like this: tree is snapshot. 
it's the kind of hierarchy, that tracks filenames 

you can get tree by commit (btw, branches are just the specific commits)

1. getting my dev's branch commit hash 
```sh
-> % [arch] cat .git/refs/heads/dev
84274ed015f3b6b69e9236d18dd0ee1db2ceeaa8
```

2. pprinted info about the commit itself
```sh
-> % [arch] git cat-file -p 84274ed015f3b6b69e9236d18dd0ee1db2ceeaa8
tree 39dfdc64ba8df7dc764286a7edf179d7940e9c89
parent 2ff633778ec99710b337f7307f8362b1366a00d9
author Paradox <> 1755767343 +0300
committer Paradox <> 1755767343 +0300
```
3. pprinted info about the tree 
```sh
-> % [arch] git cat-file -p  39dfdc64ba8df7dc764286a7edf179d7940e9c89
100644 blob e9af592d3033e0fb0e824fded66d90973927a1c9    .env.sample
100644 blob 6c05d8e19fb3840465a2b2f6f491c612c9ffc404    .gitignore
100644 blob fcc45fc5d0a5ac8026b461f18575efac97cc7eae    README.md
100644 blob 78fbdb5c636fd2407c3c9c2164eff015c2705ec0    base.Dockerfile
100644 blob bf7d9678581724e0d4ea2e5c565c844959d7aab5    docker-compose.yml
040000 tree 700e0e0b58d25fea2b1a4a4ba59aacc800a6f13b    escaped
100644 blob 0ec1a553fba8fb5ac65e4c54367d2ad39493b556    pyproject.toml
```

nice. we can see a lot of blob here. now it's time for diagram. similiar (but slightly changed structure for the strong effect) is illustrated below

```ascii

             (parent)             (parent)
  Commit A <---------- Commit B <---------- Commit C [HEAD, main]
     |                    |                    |
     |                    |                    |
  Tree A               Tree B               Tree C
   /    \                 /    \                 |
  /      \               /      \                |
Blob A   Blob B        Blob A    Blob C        Blob C
(README) (config.yml)   (README)  (README v2)   (README v2)
         |
         | -- reading blob 
         |
      "SECRET_KEY=..."
```

4. now we can pprinted blob itself (by it's pointer we get exactly the state of the blob, meaning the exact sourcecode in our case)
```sh
-> % [arch] git cat-file blob 0ec1a553fba8fb5ac65e4c54367d2ad39493b556
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "escaped"
...
```

what we can do else?
scan for diff. 
going from commit C to commit B (parent), we can produce 
```sh 
git diff --name-status <Commit B> <Commit C>
```
which basically return the mode and file, like modified or deleted (M and D accordingly)
therefore progam's understand which file has been modified/deleted
and start to hunt on it.

it issues
```sh
git show <Commit B>`:filepath
```

and then do something similiar to described above for finding blob.
the content then (contained potential `SECRET_KEY`) is restored and saved into `analysis_output` folder


## credits 

Thanks trufflehog for the great security and reconnaissance tool!
You can find it at - https://github.com/trufflesecurity/trufflehog