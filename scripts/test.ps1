# Windows equivalent of `make test`: run the pytest suite inside python:3.12-slim.
docker run --rm -v "${PWD}:/app" -w /app python:3.12-slim sh -c "pip install -q --root-user-action=ignore -r requirements.txt pytest==8.3.4 && python -m pytest -q tests $args"
