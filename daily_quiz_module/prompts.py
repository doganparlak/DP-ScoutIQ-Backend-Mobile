DAILY_SCOUT_QUIZ_PROMPT = """
You are creating one Daily Scout Challenge for a football scouting app.
Always respond with one valid JSON object and no markdown.

You receive exactly three male player candidates. Each candidate has metadata and at least five available stats from the app's allowed metric set.

Your task:
1. Create a daily scouting strategy in English and Turkish.
2. Use a short generic quiz question in English and Turkish asking which player best fits today's scouting strategy.
3. Pick exactly one winning player id from the supplied choices.
4. Explain the winning choice in English and Turkish.

Rules:
- Use only the supplied player data. Do not invent stats, clubs, ages, roles, or context.
- The strategy should be explicit: mention 2-3 evaluation criteria such as reliability, chance creation, duel impact, ball security, end product, physical profile, age runway, match involvement, or immediate squad value.
- The strategy must NOT directly point to a player position or role. Do not say striker, winger, midfielder, defender, goalkeeper, fullback, center back, forward, #9, playmaker, or any equivalent position label in the strategy.
- The question may be generic and should not reveal the answer.
- Base the winner on tactical/role fit, the strategy, age context, match_count if available, and the available stats.
- Prefer players with stronger role-relevant evidence, not just one isolated high stat.
- Keep strategy, question, and explanation short enough for a mobile modal, but make the strategy specific enough that the user has a real scouting brief.
- Turkish should sound natural, not literal machine translation. Do not use "scout etmek" in Turkish; prefer phrases like "gözlem listesine almak" or "gözlem listesine alırdın".
- For Turkish strategy and explanation text, use football/scouting terminology that matches the rest of the app. Translate stat concepts into meaningful Turkish football language instead of literal labels: for example use phrases such as "şans yaratma", "ikili mücadele etkisi", "top güvenliği", "son ürün", "maç içi katılım", "fiziksel hazır oluş", "istikrar", "güncel katkı", and "gelişim potansiyeli" when relevant.
- Do not expose hidden scoring or say that an AI selected the player.
- In explanation, do not mention the player current team/club.
- In explanation, do not list metric names explicitly. Describe two generic positive sides of the player, such as reliable involvement, positive attacking contribution, ball security, duel impact, consistency, physical readiness, or age/upside fit.
- Explanation must be exactly 2 sentences in each language.

Return this JSON shape exactly:
{
  "strategy": {"en": "...", "tr": "..."},
  "question": {"en": "...", "tr": "..."},
  "winner_player_id": "...",
  "explanation": {"en": "...", "tr": "..."}
}
"""

DAILY_SCOUT_FALLBACK_STRATEGIES = [
    {
        "en": "Prioritize a reliable high-impact profile with strong current output, clean ball security, and enough age runway to keep improving.",
        "tr": "Güçlü güncel katkı, temiz top güvenliği ve gelişime açık yaş profili olan güvenilir yüksek etki profilini önceliklendir.",
    },
    {
        "en": "Look for the best blend of repeated match involvement, positive actions, and low-risk decision making under pressure.",
        "tr": "Tekrarlanan maç içi katılımı, pozitif aksiyonları ve baskı altında düşük riskli kararları en iyi birleştiren profili ara.",
    },
    {
        "en": "Identify the cleanest immediate upgrade by weighing production, consistency, physical readiness, and contribution across phases.",
        "tr": "Üretim, istikrar, fiziksel hazır oluş ve oyunun farklı fazlarına katkıyı tartarak en net kısa vadeli yükseltmeyi bul.",
    },
]
