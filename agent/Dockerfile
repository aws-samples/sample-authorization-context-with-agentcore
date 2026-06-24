# CustomerHub CRM Agent - Container for AgentCore Runtime
FROM python:3.11-slim-bookworm

WORKDIR /app

# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy agent code
COPY agent_server.py ./

# Create non-root user and give ownership of the app directory
RUN useradd --create-home --shell /bin/bash --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Expose port 8080 (required by AgentCore Runtime)
EXPOSE 8080

# Run the agent server
CMD ["uvicorn", "agent_server:app", "--host", "0.0.0.0", "--port", "8080"]
