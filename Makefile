# Runs the test suite in the same Python image the services use (no local installs needed).
test:
	docker run --rm -v "$(CURDIR)":/app -w /app python:3.12-slim \
	  sh -c "pip install -q --root-user-action=ignore -r requirements.txt pytest==8.3.4 && python -m pytest -q tests"

.PHONY: test
