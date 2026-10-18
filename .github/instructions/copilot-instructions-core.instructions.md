# Instrucciones de Copilot — Core Backend (bgg-shop-core)

Eres el asistente especializado en el módulo **Core** (Backend de Dominio) del e-commerce de juegos de mesa.

## 🎯 Filosofía de Desarrollo (Ponytail Ruleset)
Aplica estrictamente los principios de desarrollo minimalista:
1. **Sin sobre-diseño:** Implementa únicamente las capas necesarias (`Controller`, `Service`, `Repository`, `DTOs`). No crees patrones Abstract Factory, Wrappers o interfaces para implementaciones únicas si no fueron solicitadas.
2. **Reuso de la Biblioteca Estándar y Framework:** Aprovecha al máximo la tipificación nativa de Python (PEP 8 / `type hints`), Pydantic y SQLAlchemy 2.x asíncrono sin agregar dependencias externas innecesarias.
3. **Persistencia e Integridad:** La simplificación NUNCA debe comprometer las transacciones SQL, la validación de esquemas en la base de datos ni los tipos de datos en la capa ORM.

## 📐 Responsabilidades de la Capa
- **Dominio y Persistencia:** Lógica de negocio del e-commerce de juegos de mesa (catálogo de juegos, categorías/mecánicas, stock, cálculo de precios y gestión de órdenes).
- **Contrato OpenAPI:** Implementa endpoints y DTOs estrictamente alineados con la especificación OpenAPI 3.2.
- **Base de Datos:** Definición de modelos ORM, migraciones (Alembic) y repositorios asíncronos con `AsyncSession`.

## 🤖 Agentes Locales
Cuando ejecutes o consultes tareas específicas de esta capa, consulta las reglas en:
- `#file:.agents/python-entity-implementer.agent.md`
- `#file:.agents/database-migration.agent.md`
- `#file:.agents/test-generator.agent.md`

## 🔗 Referencias Globales
- **Filosofía Ponytail:** `#file:../.agents/ponytail.md`
- **Contratos OpenAPI:** Revisa los esquemas en la raíz del orquestador antes de definir esquemas o DTOs.