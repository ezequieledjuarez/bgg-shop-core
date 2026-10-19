# 1. Imagen base oficial de Python (slim para minimizar tamaño y superficie de vulnerabilidades)
FROM python:3.12-slim

# 2. Variables de entorno de optimización de Python para contenedores
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 3. Directorio de trabajo dentro del contenedor
WORKDIR /app

# 4. Instalación de dependencias del sistema requeridas por PostgreSQL, GeoDjango y librerías C
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    gdal-bin \
    libgdal-dev \
    gettext \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 5. Copiar e instalar manifiestos de dependencias (Aprovechamiento de la capa de caché de Docker/Podman)
COPY requirements.txt /app/
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# 6. Copiar el código fuente de la aplicación
COPY . /app/

# 7. Exponer el puerto del servidor WSGI/ASGI (Django/Saleor API)
EXPOSE 8000

# 8. Comando por defecto para iniciar el servidor de desarrollo en la red local del contenedor
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]