# 🎲 BGG Shop - Core API (`bgg-shop-core`)

[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![Django 4.2](https://img.shields.io/badge/Django-4.2-green.svg)](https://www.djangoproject.com/)
[![GraphQL](https://img.shields.io/badge/GraphQL-Graphene-pink.svg)](https://graphene-python.org/)
[![Database](https://img.shields.io/badge/PostgreSQL-15-blue)](https://www.postgresql.org/)

## 📌 Descripción General

**`bgg-shop-core`** es el backend de dominio transaccional (OLTP) y fuente central de verdad para la plataforma e-commerce de juegos de mesa **BGG Shop**. 

Construido sobre **Django** y la arquitectura base de **Saleor Core**, este servicio abstrae la persitencia de datos en PostgreSQL, orquesta tareas asíncronas con Celery/Redis y expone las reglas de negocio a través de una API GraphQL unificada mediante **Graphene**.

---

## 🏗️ Arquitectura y Rol en el Sistema

Este repositorio opera de forma desacoplada dentro de la infraestructura general:
[ bgg-shop-front ]  ──(REST)──>  [ bgg-shop-bff ]  ──(GraphQL)──>  [ bgg-shop-core ]
(React / Vite)                  (Flask / Gateway)                 (Django / Saleor)
│
┌─────────┴─────────┐
▼                   ▼
[PostgreSQL]      [Redis / Celery]

* **OLTP Transaccional:** Administra usuarios, catálogo de productos, variantes y stock con integridad ACID.
* **Capa de Transporte Stricta:** Proporciona un esquema de GraphQL fuertemente tipado, previniendo problemas de *over-fetching* y *under-fetching*.
* **Asincronismo:** Procesa eventos en diferido (notificaciones, reportes masivos) mediante Celery Workers.

---

## 🛠️ Tech Stack

* **Lenguaje:** Python 3.12+
* **Framework Web:** Django 4.2+ (Saleor Architecture)
* **Capa de API:** Graphene-Django (GraphQL)
* **Base de Datos:** PostgreSQL 15
* **Broker & Cache:** Redis + Celery
* **Contenerización:** Podman / Podman Compose (Rootless)

---

## 🚀 Inicio Rápido (Entorno Local con Podman)

### Prerrequisitos
* Podman y Podman Compose instalados.
* Contenedores `db` y `redis` activos en la red compartida.

### 1. Clonar e Inicializar
```bash
git clone [https://github.com/tu-usuario/bgg-shop-core.git](https://github.com/tu-usuario/bgg-shop-core.git)
cd bgg-shop-core