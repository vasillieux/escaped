FROM python:3.10-slim

# --- System Dependencies ---
# Install git, curl (for gh and trufflehog downloads), jq (for gh --jq)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    jq \
    netcat-openbsd  \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# --- GitHub CLI (gh) Installation ---
# Refer to https://github.com/cli/cli/blob/trunk/docs/install_linux.md
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# --- TruffleHog Installation ---
# Get the latest release URL for amd64 linux
# You might want to pin this to a specific version for stability
ENV TRUFFLEHOG_VERSION=v3.77.0
RUN curl -sSfL "https://github.com/trufflesecurity/trufflehog/releases/download/${TRUFFLEHOG_VERSION}/trufflehog_${TRUFFLEHOG_VERSION#v}_linux_amd64.tar.gz" | tar -xz -C /usr/local/bin trufflehog \
    && chmod +x /usr/local/bin/trufflehog

# --- Application Setup ---
WORKDIR /app

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .
# COPY config.py ./
# COPY common_utils.py ./
# COPY submit_jobs.py ./
# COPY crawler_worker.py ./
# COPY .py ./

# Ensure scripts are executable (though python interpreter will run them)
RUN chmod +x gitsens/*.py 
RUN chmod +x gitsens/workers/*.py

# Create output directories (though volumes will likely manage them)
RUN mkdir -p /app/analysis_output/cloned_repos \
            /app/analysis_output/restored_files \
            /app/analysis_output/dangling_blobs \
            /app/analysis_output/trufflehog_findings \
            /app/analysis_output/custom_regex_findings
