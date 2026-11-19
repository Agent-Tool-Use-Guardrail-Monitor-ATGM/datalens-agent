FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
    && useradd --create-home --uid 10001 agent

USER agent
CMD ["python", "-m", "uvicorn", "statistics_agent.app:create_app", "--factory", "--loop", "statistics_agent.core.event_loop:create_event_loop", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
