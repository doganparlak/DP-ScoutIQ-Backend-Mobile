from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from player_pool_module.player_pool import search_players
from report_module.report import build_player_card_from_docs, fetch_docs_for_favorite


def _fallback_gyokeres() -> Dict[str, Any]:
    return {
        "name": "Viktor Gyökeres",
        "meta": {
            "gender": "Male",
            "age": 27,
            "nationality": "Sweden",
            "league": "Premier League",
            "team": "Arsenal",
            "height": 189,
            "weight": 90,
            "roles": ["Attacker"],
        },
        "stats": [
            {"metric": "Goals", "value": 24},
            {"metric": "Assists", "value": 8},
            {"metric": "Shots per 90", "value": 4.2},
            {"metric": "Progressive Carries", "value": 5.1},
            {"metric": "Aerial Duels Won %", "value": 48},
        ],
    }


def _content_to_player(content: Dict[str, Any]) -> Dict[str, Any]:
    name = content.get("player_name") or content.get("name") or "Viktor Gyökeres"
    roles = content.get("roles") or content.get("positions") or [content.get("position_name") or "Attacker"]
    stats = []
    skip = {
        "player_name",
        "player_name_norm",
        "name",
        "gender",
        "age",
        "nationality",
        "nationality_name",
        "league",
        "league_name",
        "league_name_norm",
        "team",
        "team_name",
        "team_name_norm",
        "height",
        "weight",
        "roles",
        "positions",
        "position_name",
    }

    for metric, value in content.items():
        if metric in skip:
            continue
        if isinstance(value, (int, float)):
            stats.append({"metric": metric, "value": value})
        elif isinstance(value, str):
            try:
                stats.append({"metric": metric, "value": float(value)})
            except ValueError:
                pass

    return {
        "name": name,
        "meta": {
            "gender": content.get("gender") or "Male",
            "age": content.get("age") or 27,
            "nationality": content.get("nationality") or content.get("nationality_name") or "Sweden",
            "league": content.get("league") or content.get("league_name") or "Premier League",
            "team": content.get("team") or content.get("team_name") or "Arsenal",
            "height": content.get("height") or 189,
            "weight": content.get("weight") or 90,
            "roles": [role for role in roles if role] or ["Attacker"],
        },
        "stats": stats,
    }


def tutorial_chat_response(db: Session) -> Dict[str, Any]:
    rows = search_players(db, {"name": "Viktor Gyökeres", "limit": 1})
    player = _content_to_player(rows[0]["content"]) if rows else _fallback_gyokeres()

    if player["name"].lower().replace("ö", "o") != "viktor gyokeres":
        player = _fallback_gyokeres()

    answer = (
        "A strong center-forward fit is Viktor Gyökeres. He gives you a powerful runner, "
        "box presence, pressing intensity, and enough mobility to lead the line in a high-pressing 4-3-3."
    )

    return {
        "response": answer,
        "data": {"players": [player]},
        "response_parts": [{"type": "text"}],
    }


def _fallback_yamal_doc() -> Dict[str, Any]:
    return {
        "id": "tutorial-lamine-yamal",
        "content": "Tutorial scouting report seed for Lamine Yamal.",
        "metadata": {
            "player_name": "Lamine Yamal",
            "gender": "Male",
            "age": 18,
            "nationality_name": "Spain",
            "team_name": "Barcelona",
            "position_name": "Right Wing",
            "height": 180,
            "weight": 72,
            "potential": 91,
            "form": 83,
            "Goals": 9,
            "Assists": 13,
            "Shots per 90": 2.4,
            "Successful Dribbles": 4.1,
            "Progressive Carries": 6.7,
            "Key Passes": 2.6,
            "Crosses": 4.8,
            "Pass Completion %": 82,
            "Ball Recoveries": 3.4,
            "Duels Won %": 45,
        },
    }


def _build_report_text(lang: str) -> str:
    is_tr = (lang or "en").lower().startswith("tr")

    if is_tr:
        role_usage = [
            "Sağ kanatta geniş başlayıp iç koridora kat ederek sol ayağıyla şut, ara pas ve ters kanat değişimi üretebilen yaratıcı bir tehdit olarak kullanılmalı.",
            "Rakip bek bire bir bırakıldığında tempo değişimi ve ilk dokunuş kalitesiyle savunma dengesini bozabilir; bu nedenle izole kanat aksiyonları için net bir çıkış noktasıdır.",
            "Topa sahip oyunlarda çizgi genişliği verirken, geçişlerde hızlı taşıma ve erken karar alma becerisiyle hücumu doğrudan ceza sahasına taşıyabilir.",
        ]
        strengths = [
            "Dar alanlarda topu koruma, yön değiştirme ve savunmacıyı ilk hamlede geçme becerisi üst seviyededir.",
            "Sol ayağıyla ceza sahasına taşıdığı toplarda hem şut hem de son pas tehdidi yaratır.",
            "Yaşına göre karar verme olgunluğu yüksektir; ne zaman çizgide kalacağını ve ne zaman içeri kat edeceğini iyi seçer.",
            "Progressive carry ve anahtar pas profili, onu sadece driplingçi değil aynı zamanda şans hazırlayan bir kanat oyuncusu yapar.",
            "Rakip savunmayı kendine çekerek iç koridordaki sekiz numara ve bek koşuları için alan açabilir.",
        ]
        weaknesses = [
            "Fiziksel temasın arttığı maçlarda topu saklama ve ikili mücadele sürekliliği hâlâ gelişim alanıdır.",
            "Savunma geçişlerinde pozisyon disiplini ve geri koşu yoğunluğu maç temposuna göre dalgalanabilir.",
            "Sağ ayağıyla son aksiyon kalitesi sol ayağı kadar güvenilir değildir; bu durum bazı savunmaların onu dış çizgiye yönlendirmesine izin verebilir.",
            "Yüksek beklenti ve yoğun dakika yükü nedeniyle performans yönetimi dikkatle planlanmalıdır.",
            "Kapalı bloklara karşı erken orta veya erken şut seçimi bazen daha sabırlı kombinasyon fırsatlarını azaltabilir.",
        ]
    else:
        role_usage = [
            "Use him as a right-sided creator who can start wide, attack the inside channel, and create shots, through balls, or switches with his left foot.",
            "When the opposing fullback is isolated, his change of pace and first touch can unbalance the defensive line, making him a clear outlet for one-v-one wing actions.",
            "In possession he provides width, while in transition his carrying speed and early decisions can move the attack directly toward the box.",
        ]
        strengths = [
            "Excellent close control in tight spaces, with the agility to change direction and beat the first defender cleanly.",
            "Creates both shooting and final-pass threat when carrying onto his left foot near the box.",
            "Shows advanced decision-making for his age, especially in choosing when to stay wide and when to move inside.",
            "His progressive carrying and key-pass profile make him more than a dribbler; he is also a chance creator.",
            "Can draw defenders toward him and open space for underlapping midfielders or fullback runs.",
        ]
        weaknesses = [
            "Still developing the strength and durability to handle repeated physical contact across a full senior season.",
            "Defensive transition discipline and recovery intensity can fluctuate with match rhythm.",
            "Final actions on his right foot are less reliable than on his left, allowing some defenders to show him outside.",
            "Because of his age and heavy expectations, minutes and workload need careful management.",
            "Against compact blocks, early crosses or early shots can sometimes replace more patient combination options.",
        ]

    return "\n".join([
        "STRENGTHS",
        *[f"- {item}" for item in strengths],
        "",
        "POTENTIAL WEAKNESSES / CONCERNS",
        *[f"- {item}" for item in weaknesses],
        "",
        "CONCLUSION",
        *[f"- {item}" for item in role_usage],
    ])


def tutorial_yamal_scouting_report(
    db: Session,
    favorite_id: str,
    lang: str = "en",
    player_identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    identity = {
        **(player_identity or {}),
        "name": "Lamine Yamal",
        "gender": (player_identity or {}).get("gender") or "Male",
        "nationality": (player_identity or {}).get("nationality") or "Spain",
    }
    docs = fetch_docs_for_favorite(db, player_identity=identity, limit_docs=30)
    if not docs:
        docs = [_fallback_yamal_doc()]

    player_card = build_player_card_from_docs(docs)
    fallback_card = _fallback_yamal_doc()["metadata"]
    player_card.setdefault("name", "Lamine Yamal")
    player_card.setdefault("gender", identity.get("gender") or "Male")
    player_card.setdefault("nationality", identity.get("nationality") or "Spain")
    player_card.setdefault("team", identity.get("team") or fallback_card["team_name"])
    player_card.setdefault("age", identity.get("age") or fallback_card["age"])
    player_card.setdefault("height", identity.get("height") or fallback_card["height"])
    player_card.setdefault("weight", identity.get("weight") or fallback_card["weight"])
    player_card.setdefault("potential", identity.get("potential") or fallback_card["potential"])
    player_card.setdefault("form", identity.get("form") or fallback_card["form"])
    if not player_card.get("roles"):
        player_card["roles"] = ["Right Wing"]

    report_text = _build_report_text(lang)
    content_json = {
        "favorite_player_id": favorite_id,
        "language": lang,
        "version": 2,
        "player_identity": identity,
        "player_card": player_card,
        "metrics_docs": docs,
        "report_text": report_text,
        "tutorial_mode": True,
    }

    return {
        "favorite_player_id": favorite_id,
        "status": "ready",
        "content": report_text,
        "content_json": content_json,
        "language": lang,
        "version": 2,
        "player": identity,
    }
