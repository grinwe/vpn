FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Copy the source into /app/bot/ so `python -m bot.bot` resolves and the
# package-relative imports (`from .config import …`) work. An empty
# __init__.py is created on the fly so we don't need to check one in
# just for the container build.
COPY . /app/bot/
RUN touch /app/bot/__init__.py
CMD ["python", "-m", "bot.bot"]
