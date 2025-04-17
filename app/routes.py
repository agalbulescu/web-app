import tempfile
from pathlib import Path

import requests
from flask import Flask, render_template, request
import os
import subprocess
from datetime import datetime
import xml.etree.ElementTree as ET

app = Flask(__name__)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/generate', methods=['POST'])
def generate():
    full_name = request.form['full_name']
    selected_games = request.form['selected_games']
    branch_name = request.form.get('branch_name', None)

    if not full_name or not selected_games:
        return "Full Name and SELECTED_GAMES are required!", 400

    sanitized_name = full_name.replace(" ", "_").lower()
    if not branch_name:
        branch_name = f"generated-{datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}"
    branch_path = f"pipeline/{sanitized_name}/{branch_name}"

    gitlab_token = os.getenv("GITLAB_ACCESS_TOKEN")
    gitlab_pipeline_token = os.getenv("GITLAB_PIPELINE_TOKEN")
    gitlab_user = os.getenv("GITLAB_USER", "oauth2")  # Optional user, 'oauth2' for token-based
    gitlab_repo_url = os.getenv("GITLAB_REPO_URL")  # e.g. https://gitlab.mydomain.com/group/repo.git
    project_id = os.getenv("GITLAB_PROJECT_ID") or "427"

    if not gitlab_token or not gitlab_repo_url:
        return "GitLab Access Token or Repo URL is missing from environment variables!", 400

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir) / "repo"

            # Clone the repo
            subprocess.run([
                "git", "clone", f"https://{gitlab_user}:{gitlab_token}@{gitlab_repo_url.lstrip('https://')}", str(repo_path)
            ], check=True)

            os.chdir(repo_path)

            # Checkout new branch
            subprocess.run(["git", "checkout", "-b", branch_path], check=True)

            # Generate pipeline file in original app context
            from app.generate_pipeline import generate_pipeline_yaml
            output_path = generate_pipeline_yaml(selected_games)

            # Copy to repo and stage
            destination_path = repo_path / ".gitlab/generated-pipeline.yml"
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "r") as src, open(destination_path, "w") as dst:
                dst.write(src.read())

            subprocess.run(["git", "config", "user.name", "Pipeline Bot"], check=True)
            subprocess.run(["git", "config", "user.email", "pipeline-bot@example.com"], check=True)
            subprocess.run(["git", "add", ".gitlab/generated-pipeline.yml"], check=True)
            subprocess.run(["git", "commit", "-m", f"Generated pipeline for {selected_games}"], check=True)
            subprocess.run(["git", "push", "-u", "origin", branch_path], check=True)

            # Trigger pipeline using requests
            gitlab_url = f"https://gitlab.mydomain.com/api/v4/projects/{project_id}/trigger/pipeline"
            payload = {
                "ref": branch_path,
                "token": gitlab_pipeline_token,
                "CI_CONFIG_PATH": ".gitlab/generated-pipeline.yml",
                "variables[SELECTED_GAMES]": selected_games  # ← important!
            }

            try:
                resp = requests.post(gitlab_url, data=payload)
                print("Status code:", resp.status_code)
                print("Response text:", resp.text)
                resp.raise_for_status()

                data = resp.json()
                pipeline_id = data.get("id")
                status = data.get("status")
                web_url = data.get("web_url", f"{gitlab_url.replace('/api/v4', '')}/pipelines/{pipeline_id}")

                return render_template(
                    "results_pending.html",
                    pipeline_id=pipeline_id,
                    status=status,
                    web_url=web_url
                )

            except requests.RequestException as e:
                return f"<h3>Pipeline trigger failed:</h3><pre>{e}</pre>", 500
    except subprocess.CalledProcessError as e:
        return f"Git operations failed: {e}", 500
    except Exception as e:
        return f"Unexpected error: {e}", 500


@app.route('/results/<pipeline_id>')
def results(pipeline_id):
    project_id = os.getenv("GITLAB_PROJECT_ID") or "427"
    gitlab_url = f"https://gitlab.mydomain.com/api/v4/projects/{project_id}/pipelines/{pipeline_id}"
    gitlab_token = os.getenv("GITLAB_ACCESS_TOKEN")

    if not gitlab_token:
        return {"status": "error", "error": "GitLab Access Token missing."}, 500

    headers = {
        "Private-Token": gitlab_token
    }

    try:
        # Fetch pipeline status
        pipeline_resp = requests.get(gitlab_url, headers=headers)
        pipeline_resp.raise_for_status()
        pipeline_data = pipeline_resp.json()

        status = pipeline_data.get("status")

        if status not in ["success", "failed"]:
            return {"status": "pending"}, 200

        # Pipeline finished, fetch the test result archive (ZIP)
        artifacts_url = f"{gitlab_url}/jobs/{pipeline_data['jobs'][0]['id']}/artifacts/test_results_bundle.zip"
        artifact_resp = requests.get(artifacts_url, headers=headers)

        if artifact_resp.status_code == 200:
            # Process the ZIP file
            with tempfile.TemporaryDirectory() as tmpdir:
                zip_path = Path(tmpdir) / "test_results_bundle.zip"
                with open(zip_path, 'wb') as f:
                    f.write(artifact_resp.content)

                # Unzip and process the results
                import zipfile
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(tmpdir)

                # Generate summary or stats from XML files (simplified)
                summary = process_test_results(tmpdir)
                return {
                    "status": "ready",
                    "summary": summary
                }, 200

        else:
            return {"status": "error", "error": "Artifacts not found."}, 404
    except requests.RequestException as e:
        return {"status": "error", "error": str(e)}, 500


def process_test_results(directory):
    # Scan for XML files
    xml_files = [f for f in os.listdir(directory) if f.endswith(".xml")]
    summary = ""

    for file in xml_files:
        file_path = os.path.join(directory, file)
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()

            # Simplified summary: Count tests, successes, failures, etc.
            tests = len(root.findall("testcase"))
            failures = len(root.findall(".//failure"))

            summary += f"File: {file}\n"
            summary += f"Total tests: {tests}, Failures: {failures}\n"
            summary += "-" * 40 + "\n"
        except ET.ParseError:
            summary += f"Error parsing file: {file}\n"

    return summary

