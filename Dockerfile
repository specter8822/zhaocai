FROM python:3.12-slim

WORKDIR /app

COPY . .

RUN pip install uv
RUN uv pip install --system --no-cache-dir -r requirements.txt

EXPOSE 7860

# 兼容 Render / Railway 这类注入 PORT 的托管平台；本地不放 PORT 时仍用 7860。
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]