FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements-gcp.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-gcp.txt
COPY . .
ENV EDMS_MODE=gcp PORT=8080
EXPOSE 8080
CMD streamlit run app/streamlit_app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true
