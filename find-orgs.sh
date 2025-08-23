#!/bin/bash

keywords=(
"llc" "inc" "web3" "incorporated" "corp" "corporation" "co" "company" "gmbh" "ltd" "plc" "pte" "sa" "bv" "oy" "ab" "sas" "tech" "digital" "cloud" "data" "ai" "ml" "blockchain" "chain" "crypto" "token" "labs" "lab" "systems" "solutions" "soft" "software" "dev" "io" "it" "api" "app" "apps" "web" "com" "online" "platform" "net" "service" "services" "hub" "security" "sec" "trust" "safe" "auth" "id" "identity" "pay" "fin" "finance" "bank" "capital" "fund" "markets" "global" "group" "enterprise" "ventures" "partners" "studio" "network" "digitalasset" "consulting" "industries" "media" "dao" "protocol" "defi" "swap" "nft" "foundation" "dex"
)

> unique_orgs.txt

# if ! [[$1]]; then 
#   echo "Usage ./find_orgs.sh total_orgs output"
# fi

echo "Searching for organizations with 10+ public repos using the GitHub API..."
echo "--------------------------------------------------------------------------"

count=0
total_orgs=$1
output=$2

for keyword in "${keywords[@]}"; do
  orgs=$(gh api "search/users?q=$keyword+in:login,name,description+type:org&per_page=100" --jq '.items[].login' 2>/dev/null)

  for org in $orgs; do
    if ! grep -q "^$org$" $output; then
      repo_count=$(gh repo list "$org" --visibility public --limit 10 --json name | jq 'length' 2>/dev/null)

      if [[ "$repo_count" -ge 10 ]]; then
        echo "Organization: $org (Found with keyword: '$keyword')"
        echo "$org" >> $output 
        count=$((count + 1))
        if [[ "$count" -ge $total_orgs ]]; then
          break 2 
        fi
      fi
    fi
  done
done

echo "Search complete. Found $count organizations matching the criteria."