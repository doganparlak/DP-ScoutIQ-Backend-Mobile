"""Enterprise tactical style labels and diagnostic metric bundles."""

ATTACK={
 'positional':('Pozisyonel Hücum','Positional Attack','Possession, pass accuracy, formation continuity and role participation are proxies; never assert spacing or off-ball positioning.'),
 'possession':('Topa Sahip Olma Odaklı Hücum','Possession-oriented Attack','Use sustained possession, passing volume and accuracy with the outfield groups carrying circulation.'),
 'direct':('Direkt Hücum','Direct Attack','Use recorded long-ball share and carrying/shooting contributions; do not invent attack speed or passing-sequence length.'),
 'transition':('Geçiş Hücumu','Transition Attack','Use recorded counterattacks when present, shots relative to passes, carrying, recovery workload and possession as a transition-style fit; do not invent timed sequences.'),
 'combination':('Kombinasyon Hücumu','Combination Attack','Passing accuracy, low long-ball share and creativity shares are proxies; no claim of observed overloads or intricate sequences.'),
 'wide':('Kanat Odaklı Hücum','Wing-oriented Attack','Use crossing and dribbling output plus contributing role groups; do not infer individual winger/fullback positions.'),
 'vertical':('Dikey Hücum','Vertical Attack','Use final-third passing, through balls, inside-box shot share and shots per pass as progression proxies; do not invent tracked central paths.'),
 'pragmatic':('Pragmatik Hücum','Pragmatic Attack','Describe measured changes in approach between periods or matches, not inferred coaching intent or opponent-specific instructions.'),
 'free':('Serbest Hücum','Free Attack','Use dribbling success, creator concentration and attacker carrying contributions as an individual-initiative fit; do not invent off-ball freedom.'),
}
DEFENCE={
 'aggressive':('Agresif Pres','Aggressive Press','Use opponent-pass-adjusted tackles and duels, forward tackling and fouls as an active-pressure fit, not measured pressing events.'),
 'high':('Önde Baskı','High Press','Use opponent offsides, opponent progression and long-ball share, possession and forward recovery shares as a high-defending fit; do not assert an observed line height.'),
 'midblock':('Orta Blok Savunma','Mid-block Defence','Use midfield interception/recovery shares, adjusted interceptions and opponent creation as a midfield-defending fit; do not invent compactness measurements.'),
 'deep':('Derin Blok Savunma','Deep-block Defence','Use clearances, defensive blocks, defender clearance shares, possession and opponent box-shot share as a goal-protection fit; do not assert a tracked block depth.'),
 'man':('Adam Odaklı Savunma','Man-oriented Defence','Use duel workload and success, defender duel contribution, tackles and fouls as a confrontation-oriented fit; do not invent marking assignments.'),
 'zone':('Alan Odaklı Savunma','Zonal Defence','Use interception activity, the distribution of recovery work, foul restraint and opponent creation/box-shot share as a space-protection fit; do not invent positional coverage.'),
 'counterpress':('Geçiş Presi','Counterpress','Use recoveries relative to opponent passes, forward/midfield recovery shares, recorded losses and opponent counterattacks as a recovery-pressure fit; do not invent post-loss timing.'),
 'reactive':('Reaktif Savunma','Reactive Defence','Statistical fit only: opponent output, possession and role recovery distribution; do not invent block depth or a deliberate reactive instruction.'),
 'pragmatic':('Pragmatik Savunma','Pragmatic Defence','Statistical fit only: measured variations in workload and recovery roles, not inferred opponent-specific plans.'),
}


DESCRIPTIONS={
 'attack':{
 'positional':('Yapılandırılmış pozisyon oyunu, alan paylaşımı ve kontrollü topa sahip olma.','Structured positional play, spacing and controlled possession'),
 'possession':('Sürdürülen topa sahip olma üstünlüğüyle maçları kontrol eder.','Controls matches through sustained possession'),
 'direct':('Daha az pas bağlantısıyla hızlı ve dikine ilerlemeyi amaçlar.','Rapid vertical progression, minimal passing sequences'),
 'transition':('Kontrataklara ve açık alanlara hücum etmeye öncelik verir.','Prioritizes counterattacks and attacking open spaces'),
 'combination':('Kısa kombinasyonlar, sayısal üstünlükler ve bağlantılı pas oyunu.','Short combinations, overloads and intricate passing'),
 'wide':('Genişliği, kanat oyuncularını ve orta fırsatlarını kullanır.','Uses width, wide players and crossing opportunities'),
 'vertical':('Merkezdeki alanları kullanarak kaleye doğru kararlı biçimde ilerler.','Aggressively progresses toward goal through central spaces'),
 'pragmatic':('Hücum stratejisini oyuncu özelliklerine ve rakibe göre uyarlar.','Adapts attacking strategies to available players and opponents'),
 'free':('Yaratıcılığı, pozisyon serbestliğini ve doğaçlamayı teşvik eder.','Encourages creativity, positional freedom and improvisation')},
 'defence':{
 'aggressive':('Yüksek şiddetli pres ve proaktif top kazanımı.','High-intensity pressing, proactive ball recovery'),
 'high':('İleri savunma çizgisiyle sahayı önde savunur.','Defends high up the pitch with an advanced defensive line'),
 'midblock':('Orta saha bölgelerinde kompakt bir savunma yapısı kurar.','Compact defensive structure in midfield zones'),
 'deep':('Kaleye yakın savunur, alan korumaya öncelik verir.','Defends close to goal, prioritizing space protection'),
 'man':('Adam odaklı eşleşmeler ve pres görevleri kullanır.','Uses man-oriented marking and pressing assignments'),
 'zone':('Pozisyon disiplinine ve alan kontrolüne öncelik verir.','Prioritizes positional discipline and space control'),
 'counterpress':('Top kaybının hemen ardından baskı uygular.','Immediately applies pressure after losing possession'),
 'reactive':('Baskıyı başlatmaktan çok rakibin aksiyonlarına karşılık verir.','Responds to opponent actions rather than initiating pressure'),
 'pragmatic':('Savunma stratejisini rakibin güçlü yönlerine göre uyarlar.','Adjusts defensive strategies based on opponent strengths')}}


# Each class has its own diagnostic team metrics and relevant outfield contributions.
BUNDLES = {
 'attack': {
  'possession': (['Ball Possession %','Passes','role:midfielder:Passes','Accurate Passes (%)'], ['Passes','Key Passes']),
  'positional': (['Final-third Pass Share (%)','Outfield Final-third Pass Share (%)','Accurate Passes (%)','role:midfielder:Passes','role:defender:Passes','Key Passes'], ['Passes','Passes In Final Third']),
  'direct': (['Long Ball Share (%)','Shots Per 100 Passes','role:attacker:Shots Total','Long Balls Won (%)'], ['Shots Total','Successful Dribbles']),
  'transition': (['Counter Attacks','Shots Per 100 Passes','role:attacker:Successful Dribbles','Recoveries Per 100 Opponent Passes','Outfield Recoveries Per 100 Opponent Passes','Shots Total'], ['Successful Dribbles','Shots Total']),
  'combination': (['role:midfielder:Key Passes','Accurate Passes (%)','Long Ball Share (%)','Key Passes'], ['Key Passes','Passes']),
  'wide': (['Cross Share (%)','Total Crosses','role:midfielder:Total Crosses','Accurate Crosses (%)','Successful Dribbles'], ['Total Crosses','Successful Dribbles']),
  'vertical': (['Through Balls','Outfield Through Balls','Final-third Pass Share (%)','Outfield Final-third Pass Share (%)','role:midfielder:Through Balls','Inside-box Shot Share (%)','Passes In Final Third'], ['Through Balls','Passes In Final Third','Key Passes']),
  'pragmatic': (['Long Ball Share (%)','Cross Share (%)','role:midfielder:Key Passes','Shots On Target (%)','Ball Possession %'], ['Key Passes','Shots Total']),
  'free': (['Creator Concentration (%)','Successful Dribbles','role:attacker:Successful Dribbles','Dribble Accuracy (%)','Key Passes'], ['Successful Dribbles','Key Passes']),
 },
 'defence': {
  'aggressive': (['Tackles Per 100 Opponent Passes','Outfield Tackles Per 100 Opponent Passes','role:attacker:Tackles','Duels Per 100 Opponent Passes','Fouls Per 100 Opponent Passes','Tackles','Total Duels'], ['Tackles','Total Duels','Fouls']),
  'high': (['Opponent Offsides','role:attacker:Ball Recovery','Opponent Final-third Pass Share (%)','Opponent Long Ball Share (%)','Opponent Shots Total'], ['Ball Recovery','Interceptions']),
  'midblock': (['role:midfielder:Interceptions','role:midfielder:Ball Recovery','Interceptions Per 100 Opponent Passes','Opponent Key Passes','Interceptions'], ['Interceptions','Ball Recovery']),
  'deep': (['Clearances','Blocked Shots','role:defender:Clearances','Opponent Inside-box Shot Share (%)','Outfield Clearances'], ['Clearances','Aerials Won','Ball Recovery']),
  'man': (['Duels Per 100 Opponent Passes','Outfield Duels Per 100 Opponent Passes','Duels Won (%)','Outfield Duels Won (%)','role:defender:Duels Won','Fouls','Total Duels'], ['Duels Won','Tackles','Total Duels']),
  'zone': (['Defensive Role Balance (%)','Interceptions Per 100 Opponent Passes','Outfield Interceptions Per 100 Opponent Passes','Opponent Inside-box Shot Share (%)','Opponent Key Passes','Interceptions'], ['Interceptions','Ball Recovery']),
  'counterpress': (['Recoveries Per 100 Opponent Passes','Outfield Recoveries Per 100 Opponent Passes','role:attacker:Ball Recovery','role:midfielder:Ball Recovery','Recovery To Loss Ratio','Outfield Recovery To Loss Ratio'], ['Ball Recovery','Interceptions']),
  'reactive': (['Opponent Shots Total','role:defender:Ball Recovery','Opponent Dangerous Attacks','Ball Possession %','Blocked Shots'], ['Ball Recovery','Clearances','Interceptions']),
  'pragmatic': (['Opponent Shots Total','role:midfielder:Tackles','role:defender:Interceptions','Opponent Key Passes'], ['Tackles','Interceptions','Ball Recovery']),
 },
}
