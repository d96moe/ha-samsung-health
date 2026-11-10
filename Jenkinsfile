// =============================================================================
// Jenkinsfile: ha-samsung-health (d96moe)
// hassfest (Home Assistant's own validator), then ruff + the full pytest suite in a
// clean Python container, then a gate that fails the build on any skipped test
// (a skip is a false green). Runs on changes (SCM poll: GitHub can't reach this
// Jenkins) and weekly, to catch new Home Assistant deprecations.
// Needs Docker on the agent.
// =============================================================================

pipeline {
    agent { label 'built-in' }

    parameters {
        string(name: 'BRANCH', defaultValue: 'main', description: 'Branch to test')
    }

    triggers {
        pollSCM('H/15 * * * *')
        cron('H 4 * * 1')
    }

    options {
        disableConcurrentBuilds()
        timestamps()
        timeout(time: 30, unit: 'MINUTES')
    }

    stages {
        stage('Checkout') {
            steps {
                git branch: "${params.BRANCH}",
                    url: 'https://github.com/d96moe/ha-samsung-health.git'
            }
        }

        stage('hassfest') {
            steps {
                sh 'docker run --rm -v "$WORKSPACE":/github/workspace ghcr.io/home-assistant/hassfest:latest'
            }
        }

        stage('Lint + pytest') {
            steps {
                sh '''
                    docker run --rm -v "$WORKSPACE":/w -w /w --user "$(id -u):$(id -g)" -e HOME=/tmp                         python:3.14-slim bash -ec "
                            python -m venv /tmp/venv
                            /tmp/venv/bin/pip install -q --upgrade pip
                            /tmp/venv/bin/pip install -q -r requirements_test.txt
                            /tmp/venv/bin/ruff check --select F,E,W,B,I,UP --ignore E501 custom_components tests
                            /tmp/venv/bin/python -m pytest tests -q -p no:cacheprovider --timeout 30 --tb=short                                 --junitxml=test-results.xml
                        "
                '''
            }
        }

        stage('Zero-skip gate') {
            steps {
                sh '''
                    SKIPPED=$(grep -oP 'testsuite[^>]*\\bskipped="\\K[0-9]+' test-results.xml | head -1 || echo 0)
                    echo "Skipped tests: ${SKIPPED:-0}"
                    if [ "${SKIPPED:-0}" -gt 0 ]; then
                        echo "ERROR: ${SKIPPED} skipped test(s). The suite must have ZERO skips."
                        exit 1
                    fi
                '''
            }
        }
    }

    post {
        always {
            junit allowEmptyResults: true, testResults: 'test-results.xml'
        }
        success { echo 'hassfest OK, all tests passed.' }
        failure { echo 'Build failed: check the log above.' }
    }
}
