.PHONY: help check fix lint types test cov run clean

PY ?= python3
SRC = src
TESTS = tests

help:
	@echo "make check  — всё, что проверяет CI (формат, линтер, типы, тесты)"
	@echo "make fix    — автоформатирование и автоисправления"
	@echo "make lint   — только ruff"
	@echo "make types  — только mypy"
	@echo "make test   — только тесты"
	@echo "make cov    — тесты с покрытием"
	@echo "make run    — запустить панель"

check: lint types test
	@echo ""
	@echo "✅ Все проверки пройдены"

fix:
	$(PY) -m ruff format $(SRC) $(TESTS)
	$(PY) -m ruff check $(SRC) $(TESTS) --fix

lint:
	$(PY) -m ruff format --check $(SRC) $(TESTS)
	$(PY) -m ruff check $(SRC) $(TESTS)

types:
	$(PY) -m mypy $(SRC)

test:
	$(PY) -m pytest -q

cov:
	$(PY) -m pytest --cov=tvhost --cov-report=term

run:
	PYTHONPATH=$(SRC) $(PY) -m tvhost

clean:
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info coverage.xml .coverage
