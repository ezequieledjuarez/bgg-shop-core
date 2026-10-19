# bgg-shop-core/saleor/graphql/api.py
import graphene
from django.db import connection


class Product(graphene.ObjectType):
    bgg_id = graphene.Int(required=True)
    name = graphene.String(required=True)
    release_year = graphene.Int(required=True)
    min_players = graphene.Int(required=True)
    max_players = graphene.Int(required=True)
    playing_time_minutes = graphene.Int(required=True)
    min_age = graphene.Int(required=True)
    weight = graphene.Float(required=True)
    category = graphene.List(graphene.NonNull(graphene.String), required=True)
    price = graphene.Float(required=True)
    publisher = graphene.String(required=True)
    image_url = graphene.String(required=True)


class ProductPage(graphene.ObjectType):
    items = graphene.List(graphene.NonNull(Product), required=True)
    page = graphene.Int(required=True)
    page_size = graphene.Int(required=True)
    total = graphene.Int(required=True)
    total_pages = graphene.Int(required=True)


class Query(graphene.ObjectType):
    hello = graphene.String(
        default_value="Saleor API activa y respondiendo OK"
    )

    def resolve_hello(self, info):
        return "Saleor API activa y respondiendo OK"

    products = graphene.Field(
        ProductPage,
        page=graphene.Int(default_value=1),
        page_size=graphene.Int(default_value=20),
        search=graphene.String(),
        year_published=graphene.Int(),
        min_players=graphene.Int(),
        max_players=graphene.Int(),
        category_id=graphene.Int(),
        price_min=graphene.Float(),
        price_max=graphene.Float(),
    )

    def resolve_products(
        self, info, page=1, page_size=20, search=None, year_published=None,
        min_players=None, max_players=None, category_id=None,
        price_min=None, price_max=None,
    ):
        if page < 1 or page_size < 1:
            raise ValueError("page and pageSize must be positive integers")
        if min_players is not None and max_players is not None and min_players > max_players:
            raise ValueError("minPlayers must be less than or equal to maxPlayers")
        if price_min is not None and price_max is not None and price_min > price_max:
            raise ValueError("priceMin must be less than or equal to priceMax")

        with connection.cursor() as cursor:
            conditions = []
            parameters = []
            if search:
                conditions.append("(LOWER(name) LIKE LOWER(%s) OR LOWER(publisher) LIKE LOWER(%s))")
                term = f"%{search}%"
                parameters.extend([term, term])
            if year_published is not None:
                conditions.append("release_year = %s")
                parameters.append(year_published)
            if min_players is not None:
                conditions.append("max_players >= %s")
                parameters.append(min_players)
            if max_players is not None:
                conditions.append("min_players <= %s")
                parameters.append(max_players)
            if category_id is not None:
                category_names = {
                    1: "Juegos de Estrategia",
                    2: "Juegos Temáticos",
                    3: "Juegos Familiares",
                }
                category_name = category_names.get(category_id)
                if category_name is None:
                    conditions.append("1 = 0")
                else:
                    conditions.append(
                        "(category = %s OR category LIKE %s OR category LIKE %s OR category LIKE %s)"
                    )
                    parameters.extend([
                        category_name, f"{category_name},%", f"%, {category_name}", f"%, {category_name},%"
                    ])
            if price_min is not None:
                conditions.append("price_amount >= %s")
                parameters.append(price_min)
            if price_max is not None:
                conditions.append("price_amount <= %s")
                parameters.append(price_max)

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            cursor.execute(f"SELECT COUNT(*) FROM product_boardgame {where_clause}", parameters)
            total = cursor.fetchone()[0]
            cursor.execute(
                f"""
                SELECT bgg_id, name, release_year, min_players, max_players,
                       playing_time_minutes, min_age, weight, category,
                       price_amount, publisher, image_url
                FROM product_boardgame
                {where_clause}
                ORDER BY id
                LIMIT %s OFFSET %s
                """,
                [*parameters, page_size, (page - 1) * page_size],
            )
            columns = [column[0] for column in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

        items = [
            Product(
                bgg_id=row["bgg_id"],
                name=row["name"],
                release_year=row["release_year"],
                min_players=row["min_players"],
                max_players=row["max_players"],
                playing_time_minutes=row["playing_time_minutes"],
                min_age=row["min_age"],
                weight=row["weight"],
                price=row["price_amount"],
                publisher=row["publisher"],
                image_url=row["image_url"],
                category=[
                    category.strip()
                    for category in (row["category"] or "").split(",")
                    if category.strip()
                ],
            )
            for row in rows
        ]
        return ProductPage(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=(total + page_size - 1) // page_size,
        )


schema = graphene.Schema(query=Query)