FROM python:3.12-slim

WORKDIR /app

COPY datalake/requirements.txt /app/datalake/requirements.txt
RUN pip install --no-cache-dir -r /app/datalake/requirements.txt

COPY datalake/ /app/datalake
COPY kalshi_btc15m_bot/ /app/kalshi_btc15m_bot
COPY backtest/ /app/backtest

ENV PYTHONPATH=/app \
    DATALAKE_LOG_FORMAT=console

# Default: show the capture loop's help. Override per stage, e.g.
#   docker run lake python -m datalake.capture --iterations 5
#   docker run lake python -m datalake.validate --all
CMD ["python", "-m", "datalake.capture", "--help"]
