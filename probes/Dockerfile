# Stateless probe agent. Designed to run from the *outside* of the VPN
# service — typically on a residential connection in the region you want
# to observe blocking from. Single file, no DB, no SSH keys. Safe to run
# ten copies in parallel.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py ./

# Run as non-root: nothing here needs privileges.
RUN useradd --system --no-create-home --shell /usr/sbin/nologin probe
USER probe

ENV PYTHONUNBUFFERED=1 \
    LOG_LEVEL=INFO

CMD ["python", "agent.py"]
