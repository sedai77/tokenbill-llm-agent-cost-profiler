# Convenience targets; everything is runnable directly with uv too.
UV_DEV := uv run --python 3.12 --extra dev

.PHONY: demo test lint clean

demo:  ## Full offline pipeline: planted-waste traces -> analyzer -> simulator -> report.html
	uv run tokenbill demo -o report.html

test:
	$(UV_DEV) pytest -q

lint:
	$(UV_DEV) ruff check .

clean:
	rm -rf dist build .pytest_cache .ruff_cache .venv report.html
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
