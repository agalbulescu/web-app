import os
import yaml
from collections import defaultdict
from pathlib import Path

GAMES = [
    "game_01", "game_02", "game_03", "game_04", "game_05", "game_06", "game_07", "game_08", 
    "game_09", "game_10", "game_11", "game_12", "game_13", "game_14", "game_15"
]

RESOURCE_GROUPS = 5
BASE_DIR = Path(__file__).parent  # Points to /app
BASE_PIPELINE_PATH = BASE_DIR / "templates/base_pipeline.yml"
# The output pipeline file in your app; later it will be copied into the cloned repo.
OUTPUT_PATH = BASE_DIR / "generated-pipeline.yml"
# Path to the game test template that we want to inline
GAME_TEMPLATE_PATH = BASE_DIR / "templates/game_job_template.yml"


def parse_selected_games(selected_games: str):
    parsed = defaultdict(list)
    for entry in selected_games.split(","):
        if not entry.strip():
            continue
        parts = entry.strip().split(":")
        game = parts[0]
        suites = parts[1:] if len(parts) > 1 else []
        parsed[game].extend(suites)
    return parsed


def assign_resource_group(index):
    return f"game_group_{index % RESOURCE_GROUPS + 1}"


def generate_game_job(game: str, resource_group: str, suites: list) -> tuple:
    # Load the game test template YAML from file
    with open(GAME_TEMPLATE_PATH) as tf:
        template_yaml = yaml.safe_load(tf)

    script = [
        f'echo "Running {game} tests"',
        f'{game.upper()}_SECTION=$(echo "$SELECTED_GAMES" | grep -oP \'{game}:\\K[^,]*\')',
        f'if [ -z "${{{game.upper()}_SECTION}}" ]; then echo "No {game} tests selected."; exit 0; fi',
        f'{game.upper()}_SUITES=$(echo "${{{game.upper()}_SECTION}}" | tr \':\' \' \')',
        'PYTEST_CMDS=()',
        # This block will be ONE string element
        f'''\
    for SUITE in ${game.upper()}_SUITES; do
      case $SUITE in
        sanity)
          PYTEST_CMDS+=("pytest tests/{game}/desktop/ tests/{game}/mobile/ -m sanity")
          ;;
        smoke)
          PYTEST_CMDS+=("pytest tests/{game}/desktop/ tests/{game}/mobile/ -m smoke")
          ;;
        all)
          PYTEST_CMDS+=("pytest tests/{game}/desktop/ tests/{game}/mobile/")
          ;;
        payouts)
          PYTEST_CMDS+=("pytest tests/{game}/desktop/ tests/{game}/mobile/ -k payouts")
          ;;
        analytics)
          PYTEST_CMDS+=("pytest tests/{game}/desktop/ tests/{game}/mobile/ -m analytics")
          ;;
        smapp)
          PYTEST_CMDS+=("pytest tests/{game}/desktop/ -k smapp")
          ;;
        desktop)
          PYTEST_CMDS+=("pytest tests/{game}/desktop/")
          ;;
        mobile)
          PYTEST_CMDS+=("pytest tests/{game}/mobile/")
          ;;
        desktop_payouts)
          PYTEST_CMDS+=("pytest tests/{game}/desktop/ -m payouts")
          ;;
        mobile_payouts)
          PYTEST_CMDS+=("pytest tests/{game}/mobile/ -m payouts")
          ;;
        desktop_ui)
          PYTEST_CMDS+=("pytest tests/{game}/desktop/ -m ui")
          ;;
        mobile_ui)
          PYTEST_CMDS+=("pytest tests/{game}/mobile/ -k ui")
          ;;
        desktop_analytics)
          PYTEST_CMDS+=("pytest tests/{game}/desktop/ -m analytics")
          ;;
        mobile_analytics)
          PYTEST_CMDS+=("pytest tests/{game}/mobile/ -m analytics")
          ;;
        *)
          echo "Unknown suite: $SUITE, skipping."
          exit 1
          ;;
      esac
    done
    '''
        ,
        'echo "Running commands:"',
        f'for CMD in "${{PYTEST_CMDS[@]}}"; do',
        '  echo "$CMD"',
        f'  eval "$CMD --junitxml=reports/results_{game}.xml || true"',
        'done'
    ]

    # Merge the template content with our job-specific settings
    job_dict = {}
    job_dict.update(template_yaml)  # copy all keys from the template
    job_dict["resource_group"] = resource_group
    job_dict["script"] = script
    job_dict["rules"] = [{"if": f"$SELECTED_GAMES =~ /{game}:/", "when": "always"}]
    job_dict["artifacts"] = {
        "untracked": True,
        "paths": [f"reports/results_{game}.xml"]
    }

    return f"test_{game}", {f"test_{game}": job_dict}


def generate_pipeline_yaml(selected_games):
    parsed_games = parse_selected_games(selected_games)
    if not parsed_games:
        raise ValueError("No games selected.")

    # Load the base pipeline YAML that doesn't change
    with open(BASE_PIPELINE_PATH) as f:
        base_yaml = yaml.safe_load(f)

    # Get the dynamic test jobs
    game_jobs = {}
    test_job_names = []

    for idx, (game, suites) in enumerate(parsed_games.items()):
        group = assign_resource_group(idx)
        job_name, job_dict = generate_game_job(game, group, suites)
        test_job_names.append(job_name)
        # game_jobs.update(job_dict)

        # Safely add all jobs returned (even if more than one in the future)
        for name, job in job_dict.items():
            game_jobs[name] = job

    # Merge the static and dynamic parts of the pipeline
    base_yaml.update(game_jobs)

    # Add rerun_failed_tests job
    rerun_job = {
        "rerun_failed_tests": {
            "stage": "rerun_failed",
            "image": "escuxezg0/pypipe-debian:latest",
            "script": [
                "mkdir -p combined_reports",
                "mkdir -p reports",
                "cat reports/results_*.xml > combined_reports/all_results.xml || true",
                (
                    "bash -c '\n"
                    "FAILED_TESTS=$(xmllint --xpath \"//testcase[./failure]/@name\" "
                    "combined_reports/all_results.xml 2>/dev/null | "
                    "sed -E \"s/name=\\\\\\\"([^\\\\\\\"]+)\\\\\\\"/\\1/g\" | tr \"\\n\" \" \")\n"
                    "echo \"Detected failed tests: $FAILED_TESTS\"\n"
                    "if [ -n \"$FAILED_TESTS\" ]; then\n"
                    "  pytest -k \"$FAILED_TESTS\" --junitxml=reports/rerun_results.xml || true\n"
                    "else\n"
                    "  echo \"No failed tests to re-run.\"\n"
                    "  touch reports/rerun_results.xml\n"
                    "fi\n"
                    "zip -r reports/test_results_bundle.zip reports/*.xml reports/combined_reports/*.xml || true\n"
                    "'"
                )
            ],
            "dependencies": test_job_names,
            "artifacts": {
                "paths": [
                    "reports/rerun_results.xml",
                    "reports/test_results_bundle.zip"
                ],
                "when": "always"
            },
            "rules": [{"when": "always"}]
        }
    }

    base_yaml.update(rerun_job)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        yaml.dump(base_yaml, f, sort_keys=False)

    return OUTPUT_PATH

