.PHONY: up down test logs

# Start the stack (db, mock-ai, api, worker) in the background
up:
	docker compose up --build -d

down:
	docker compose down

# Runs pytest inside the app image against a dedicated test database on the Compose db
test:
	docker compose run --rm --build api python -m pytest $(ARGS)

logs:
	docker compose logs -f
