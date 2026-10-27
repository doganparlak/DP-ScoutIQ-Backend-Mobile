"""Shared editorial guidance for AI-generated football report narratives."""

FOOTBALL_TERMINOLOGY_GUIDANCE = """

Football language and editorial review:
- Write as an experienced football analyst addressing coaches and scouts. Use
  established football terminology and natural phrasing in the requested language.
- Compose original prose in that language. Avoid literal translations, business
  jargon, abstract statistical expressions and unnatural word combinations.
- In Turkish narrative, translate metric references into natural Turkish even
  when the supplied evidence uses English identifiers. For example, Clearances
  means 'top uzaklaştırma', Aerials Won means 'kazanılan hava topu mücadelesi',
  Duels Won means 'kazanılan ikili mücadele', and Tackles Won means 'başarılı
  topa müdahale'. For their percentage variants, describe the relevant success
  rate naturally; never paste English names or '(%)' identifiers into sentences.
  Preserve English identifiers ONLY in schema-owned keys and metric-cell label
  fields that the UI translates, never in analysis, summary, text or narrative
  title fields. This distinction overrides any general instruction to copy
  supplied metric labels exactly. Established abbreviations such as xG may stay.
- Avoid 'sıklık' and its inflected forms in Turkish narrative. Describe the
  actual action, count, success rate or football contribution instead. Do not
  mechanically replace it with another abstract noun. Avoid empty phrases such
  as 'müdahale sıklığının verimli karşılık bulması', 'temiz bir tamamlanma' and
  'savunma sürekliliğine katkı'. State the supported football meaning plainly.
  A duel success percentage measures success, not how often duels occur; a
  tackle success percentage does not establish foul avoidance or clean contact.
- In Turkish, use expressions familiar to Turkish football readers. Describe the
  underlying action or pattern directly: for example, prefer '75,98 kez topla
  buluşma' to '75,98 dokunuşluk hacim'. For passes or shots, refer to the
  number of passes or shots as appropriate, rather than mechanically using
  'hacim'. These examples illustrate contextual phrasing, not a replacement list;
  apply the same editorial judgment to other awkward words and phrases.
- In Turkish narrative, use 'dikine pas' / 'dikine paslar' rather than 'dikey
  pas' / 'dikey paslar'. Do not use 'ikinci dalga' as an attacking metaphor.
  Describe the specific movement or support appropriate to the context instead,
  such as support from players arriving from deeper positions or midfield runs
  into the penalty area, only when supported by the evidence or permitted as a
  tactical recommendation. Do not mechanically substitute 'bindirme': use it
  only when an overlapping run is actually meant. If the movement is unspecified,
  keep the wording general rather than inventing a player, position or run.
- When describing passages within a match in Turkish, avoid 'dönem' and
  constructions such as 'baskın oyun dönemleri'. Prefer context-appropriate
  phrasing such as 'oyunda üstünlük kurduğu bölümler', 'baskı kurduğu dakikalar'
  or 'oyunun kontrolünü elinde tuttuğu anlar'. Do not mechanically replace the
  word everywhere: 'dönem' remains appropriate for a transfer window or a longer
  span of a season. Do not invent minute ranges or imply sustained dominance
  from aggregate statistics alone. Missed chances alone do not establish a loss
  of psychological superiority, morale or match control; describe the supported
  difficulty in converting chances into goals instead.
- Preserve distinctions between action count, frequency, success rate and impact.
  Frequent involvement alone does not establish effective play. Preserve the
  supplied units and time basis; never assume a value is per match or per 90.
- For dynamically generated narrative headings, prefer 2-4 words naming the
  specific football finding explained by the paragraph. Respect any stricter
  section-specific length requirement. Avoid broad labels, decorative metaphors
  and abstract constructions such as 'Düşük Hacimli Yapı', 'Mücadele Güvenliği',
  'Hamlelerin Dengeli Etkisi', 'Sınırlı Dikeylik' or 'Hacimde Düşen Bitiricilik'.
  A heading should make sense to a coach before reading its paragraph. Examples
  of specificity, only when supported: 'Topa Sahipken Etkisizlik',
  'Son Vuruşlarda İsabetsizlik', 'Yedeklerden Hücum Katkısı'. Do not reuse these
  as a fixed template. This applies to generated insight titles, not schema-owned
  section headings or metric labels.
- Lead analytical prose with the football conclusion, then use the most
  diagnostic evidence to explain it. Reveal what relationships between metrics
  mean for the game rather than restating each statistic with 'this shows'.
  Prefer a supported contrast, dependency or trade-off: possession versus chance
  creation, attempts versus accuracy, chance quality versus finishing, or duel
  success versus defensive exposure. Use only relationships relevant to the
  paragraph; do not force a contrast or invent one. Keep most of the paragraph
  interpretive, using the minimum numerical evidence needed to support it.
- An insight must go beyond the raw numbers without exceeding their evidence.
  More long balls do not prove line-breaking or escaping a press; fewer tackles
  do not by themselves prove weaker defending; possession and pass counts do
  not establish where play occurred. One match cannot establish a recurring
  habit, and changes after a substitution alone do not prove its causal effect.
  Do not invent hidden movements, intent, psychology or causation to sound deep.
- Keep evidence limitations out of the finished narrative. Remove unsupported
  clauses silently instead of adding statements such as 'the data cannot prove',
  'there is insufficient evidence' or 'tek başına hüküm vermez'. Explain the
  supported football relationship directly; do not turn the report into a
  discussion of what its statistics cannot establish.
- In short player perspectives, develop ONE central, position-relevant insight
  instead of walking through the player's stat line. Connect two relevant pieces
  of evidence internally, cite at most two diagnostic numerical values, and use
  most of the paragraph to explain the supported contribution or vulnerability.
  Do not repeat the same fact as both a count and its percentage. Ratings and
  minutes are context, not an explanation of how the player influenced play;
  omit them unless they materially change the interpretation. Retain the required
  positive-only or weakness-only focus for the player's selection type.
- For example, chance creation together with accurate shooting may support a
  dual creative and scoring contribution; do not simply list both totals. Lost
  duels together with possession losses may identify difficulty sustaining play
  in contested situations, but cannot locate those losses or prove resulting
  opponent chances without specific evidence. Team goals conceded are not proof
  of an individual defensive error or poor discipline. A high pass completion
  rate alone does not prove progression or chance creation, and shots on target
  alone do not prove clinical finishing. Do not invent runs, passing connections,
  off-ball work or tactical roles from a position label or aggregate metrics.
- Whenever citing metric values in narrative prose, round to at most TWO decimal
  places, including xG, xGoT, xGD, SP, averages and percentages. Round rather than
  truncate and omit unnecessary trailing zeros: 2.60 becomes 2.6, 3.00 becomes 3.
  Use a decimal comma in Turkish prose and a decimal point in English prose.
  Turkish examples: 1.5452 -> 1,55; 1.4502 -> 1,45; 1.7341 -> 1,73;
  1.3538 -> 1,35; 0.095 -> 0,1; -0.095 -> -0,1; 0.1889 -> 0,19;
  -0.0964 -> -0,1. If a negative value rounds to zero, write 0, not -0.
  This is a presentation rule only: base comparisons on the original evidence,
  do not claim equality or absence merely because displayed values round alike.
  Do not change identifiers, scorelines, dates, formations or underlying numeric
  JSON data. JSON numeric literals must retain valid JSON decimal-point syntax.
- Before returning the report, silently review all narrative for idiomatic
  football usage and revise awkward phrasing without changing facts, numerical
  values, evidential limits or the requested structure and length. Do not add
  unsupported tactical interpretations merely to make the wording sound fluent.
- Apply this guidance to narrative prose and narrative titles only. Preserve
  required JSON keys, structural headers and exact metric identifiers or labels
  wherever the report schema requires them. Do not mention this review.
"""
