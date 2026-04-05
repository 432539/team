FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir flask requests gunicorn curl_cffi

COPY app.py pay.py checkout.py ./
COPY templates/ templates/

EXPOSE 5080

CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:5080", "--timeout", "600", "app:app"]
