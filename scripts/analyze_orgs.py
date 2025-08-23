import sys
import json
import subprocess
import os

GRAPHQL_QUERY = """
query OrgRecon($orgLogin: String!) {
  organization(login: $orgLogin) {
    name
    repositories(privacy: PUBLIC) {
      totalCount
    }
    popularRepos: repositories(
      first: 10
      isFork: false
      privacy: PUBLIC
      orderBy: {field: STARGAZERS, direction: DESC}
    ) {
      nodes {
        name
        description
        url
        stargazerCount
        forkCount
        pushedAt
        primaryLanguage {
          name
        }
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          totalSize
          edges {
            size
            node {
              name
            }
          }
        }
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 1) { 
                totalCount # Total commit count for the entire branch.
                nodes {      # The single most recent commit.
                  oid
                  committedDate
                }
              }
            }
          }
        }
        refs(refPrefix: "refs/heads/") {
          totalCount
        }
      }
    }
  }
}
"""

def analyze_organization(org_name):
    print(f"[*] Analyzing organization: {org_name}")
    command = [
        "gh", "api", "graphql",
        "-f", f"orgLogin={org_name}",
        "--raw-field", f"query={GRAPHQL_QUERY}"
    ]
    try:
        process = subprocess.run(
            command, capture_output=True, text=True, check=True, encoding='utf-8'
        )
        return json.loads(process.stdout)
    except subprocess.CalledProcessError as e:
        error_output = e.stderr
        if "Could not resolve to an Organization" in error_output:
            print(f"[!] Error: Organization '{org_name}' not found. Skipping.")
        else:
            print(f"[!] An error occurred for '{org_name}': {error_output.strip()}")
        return None
    except json.JSONDecodeError:
        print(f"[!] Error: Failed to decode JSON response for '{org_name}'. Skipping.")
        return None

def format_results(api_data):
    org_data = api_data.get("data", {}).get("organization")
    if not org_data:
        return None
        
    total_stars_top_10 = sum(repo.get("stargazerCount", 0) for repo in org_data["popularRepos"]["nodes"])
    total_commits_top_10 = 0 

    result = {
        "organization_name": org_data.get("name"),
        "total_public_repos": org_data.get("repositories", {}).get("totalCount", 0),
        "total_stars_top_10_repos": total_stars_top_10,
        "top_10_popular_repos": []
    }

    for repo in org_data["popularRepos"]["nodes"]:
        languages = {}
        lang_data = repo.get("languages", {})
        total_size = lang_data.get("totalSize", 1)
        if total_size > 0:
            for edge in lang_data.get("edges", []):
                lang_name = edge["node"]["name"]
                proportion = round((edge["size"] / total_size) * 100, 2)
                languages[lang_name] = f"{proportion}%"
        
        last_commit = None
        commit_count = 0
        try:
            target = repo["defaultBranchRef"]["target"]
            commit_count = target["history"]["totalCount"]
            total_commits_top_10 += commit_count
            
            commit_node = target["history"]["nodes"][0]
            last_commit = {
                "hash": commit_node.get("oid"),
                "date": commit_node.get("committedDate")
            }
        except (TypeError, IndexError, KeyError):
            last_commit = {"hash": None, "date": None}
            commit_count = 0


        language = repo.get("primaryLanguage", {})
        if language is not None: 
            language = language.get("name")
        repo_details = {
            "name": repo.get("name"),
            "url": repo.get("url"),
            "description": repo.get("description"),
            "stars": repo.get("stargazerCount"),
            "forks": repo.get("forkCount"),
            "commit_count": commit_count, 
            "primary_language": language,
            "language_proportions": languages,
            "last_commit": last_commit,
            "branch_count": repo.get("refs", {}).get("totalCount", 0),
            "last_activity_at": repo.get("pushedAt")
        }
        result["top_10_popular_repos"].append(repo_details)
    
    result["total_commits_top_10_repos"] = total_commits_top_10
    return result

def main():
    if len(sys.argv) != 3:
        print("Usage: python analyze_orgs.py <input_file_with_orgs> <output_json_file>")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2]

    if not os.path.exists(input_file):
        print(f"Error: Input file '{input_file}' not found.")
        sys.exit(1)

    with open(input_file, 'r') as f:
        org_names = [line.strip() for line in f if line.strip()]

    all_results = []
    for org_name in org_names:
        raw_data = analyze_organization(org_name)
        if raw_data:
            formatted_data = format_results(raw_data)
            if formatted_data:
                all_results.append(formatted_data)

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)

    print(f"\n[+] Analysis complete. Results saved to '{output_file}'")

if __name__ == "__main__":
    main()
