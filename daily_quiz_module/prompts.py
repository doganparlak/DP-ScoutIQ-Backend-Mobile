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
- The strategy should be explicit: pick exactly 3 evaluation criteria from this paired list and use the matching Turkish terms in the Turkish strategy when those criteria are selected: Reliability Under Pressure / Baskı Altında Güvenilirlik; Contribution in Key Moments / Kritik Anlardaki Katkı; Defensive Recovery Speed / Savunmaya Dönüş Hızı; Execution Consistency / Uygulama Tutarlılığı; Influence Between the Lines / Hatlar Arasındaki Etki; Space Exploitation / Boş Alan Kullanımı; Support Play / Destek Oyunu; Ball Security / Top Güvenliği; Offensive Involvement / Hücum Katılımı; Defensive Presence / Savunmadaki Varlığı; Positional Discipline / Pozisyon Disiplini; Spatial Awareness / Alan Farkındalığı; Transition Efficiency / Geçiş Verimliliği; Playmaking Influence / Oyun Kurucu Etkisi; Territorial Advancement / Saha Kazandırma Yeteneği.
- The user message includes today's required theme and allowed criteria. You MUST build the strategy from that theme and pick criteria only from the supplied allowed criteria for today.
- Do not choose defensive criteria unless today's required theme explicitly includes them.
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

DAILY_SCOUT_THEMES = [
    {
        "key": "attacking_impact",
        "label": "Attacking impact",
        "allowed_criteria": [
            "Contribution in Key Moments / Kritik Anlardaki Katkı",
            "Offensive Involvement / Hücum Katılımı",
            "Space Exploitation / Boş Alan Kullanımı",
            "Influence Between the Lines / Hatlar Arasındaki Etki",
            "Execution Consistency / Uygulama Tutarlılığı",
        ],
        "fallback_strategy": {
            "en": "Prioritize a player who creates danger through offensive involvement, key-moment contribution, and sharp execution around advanced spaces.",
            "tr": "Hücum katılımı, kritik anlardaki katkı ve ileri alanlarda net uygulama kalitesiyle tehlike yaratan oyuncuyu önceliklendir.",
        },
    },
    {
        "key": "playmaking_control",
        "label": "Playmaking and control",
        "allowed_criteria": [
            "Playmaking Influence / Oyun Kurucu Etkisi",
            "Ball Security / Top Güvenliği",
            "Support Play / Destek Oyunu",
            "Territorial Advancement / Saha Kazandırma Yeteneği",
            "Execution Consistency / Uygulama Tutarlılığı",
        ],
        "fallback_strategy": {
            "en": "Look for a player who controls rhythm through playmaking influence, ball security, and reliable support play.",
            "tr": "Oyun kurucu etkisi, top güvenliği ve güvenilir destek oyunuyla ritmi kontrol eden oyuncuyu ara.",
        },
    },
    {
        "key": "transition_engine",
        "label": "Transition engine",
        "allowed_criteria": [
            "Transition Efficiency / Geçiş Verimliliği",
            "Territorial Advancement / Saha Kazandırma Yeteneği",
            "Reliability Under Pressure / Baskı Altında Güvenilirlik",
            "Space Exploitation / Boş Alan Kullanımı",
            "Contribution in Key Moments / Kritik Anlardaki Katkı",
        ],
        "fallback_strategy": {
            "en": "Identify a player who turns pressure into forward momentum through transition efficiency, territorial gain, and reliable decisions.",
            "tr": "Geçiş verimliliği, saha kazandırma etkisi ve güvenilir kararlarla baskıyı ileri momentuma çeviren oyuncuyu belirle.",
        },
    },
    {
        "key": "defensive_reliability",
        "label": "Defensive reliability",
        "allowed_criteria": [
            "Defensive Recovery Speed / Savunmaya Dönüş Hızı",
            "Defensive Presence / Savunmadaki Varlığı",
            "Positional Discipline / Pozisyon Disiplini",
            "Spatial Awareness / Alan Farkındalığı",
            "Reliability Under Pressure / Baskı Altında Güvenilirlik",
        ],
        "fallback_strategy": {
            "en": "Focus on a player who adds defensive reliability through recovery speed, positional discipline, and awareness under pressure.",
            "tr": "Savunmaya dönüş hızı, pozisyon disiplini ve baskı altında farkındalıkla savunma güvenilirliği katan oyuncuya odaklan.",
        },
    },
    {
        "key": "balanced_value",
        "label": "Balanced value",
        "allowed_criteria": [
            "Execution Consistency / Uygulama Tutarlılığı",
            "Reliability Under Pressure / Baskı Altında Güvenilirlik",
            "Support Play / Destek Oyunu",
            "Contribution in Key Moments / Kritik Anlardaki Katkı",
            "Ball Security / Top Güvenliği",
        ],
        "fallback_strategy": {
            "en": "Prioritize a balanced profile with consistency, pressure reliability, support play, and useful contribution in key moments.",
            "tr": "Tutarlılık, baskı altında güvenilirlik, destek oyunu ve kritik anlarda faydalı katkıyı birleştiren dengeli profili önceliklendir.",
        },
    },
]
