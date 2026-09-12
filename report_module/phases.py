"""Enterprise match-phase taxonomy and scoring, exposed as report distributions.

Keep the taxonomy, metric ranges and scoring helpers aligned with enterprise
report_module/report.py. Distributions are computed without model interpretation.
"""
from __future__ import annotations
import math
from typing import Any, Dict, List, Optional, Tuple
from constants_module.constants import ROLE_LONG_TO_SHORT, ROLE_SHORT_TO_LONG

WIDE_PHASE_ROLES = {"LW", "RW", "LM", "RM"}

FULLBACK_PHASE_ROLES = {"LB", "RB"}

OUTFIELD_PHASES = ["Build-up", "Progression", "Final Third", "High Block", "Mid Block", "Low Block"]

GOALKEEPER_PHASES = ["Build-up", "Low Block"]

PHASE_ROLE_TAXONOMY: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
    "GK": {
        "Build-up": [
            {
                "name": "Safe-Passing Goalkeeper",
                "description": "Savunmadan çıkışta düşük riskli pasları tercih eder; stoperler ve beklerle kısa pas bağlantısı kurarak takımın topa sahip olmasını sürdürür.",
                "metrics": [
                    "Passes",
                    "Accurate Passes",
                    "Accurate Passes (%)",
                    "Touches",
                    "Backward Passes",
                    "Possession Lost",
                    "Turn Over",
                    "Error Lead To Shot",
                    "Error Lead To Goal",
                ],
            },
            {
                "name": "Long-Distribution Goalkeeper",
                "description": "Rakibin baskısını uzun paslarla aşarak topu doğrudan orta saha veya hücum hattına ulaştırmaya çalışır.",
                "metrics": [
                    "Long Balls",
                    "Long Balls Won",
                    "Long Balls Won (%)",
                    "Passes",
                    "Accurate Passes",
                    "Accurate Passes (%)",
                    "Possession Lost",
                    "Turn Over",
                ],
            },
            {
                "name": "Playmaking Goalkeeper",
                "description": "Kısa ve uzun pasları birlikte kullanır; rakibin baskısına göre çıkış yönünü belirler ve takımın oyun kurulumuna aktif olarak katılır.",
                "metrics": [
                    "Touches",
                    "Passes",
                    "Accurate Passes",
                    "Accurate Passes (%)",
                    "Long Balls",
                    "Long Balls Won",
                    "Long Balls Won (%)",
                    "Backward Passes",
                    "Possession Lost",
                    "Error Lead To Shot",
                ],
            },
        ],
        "Low Block": [
            {
                "name": "Line Goalkeeper",
                "description": "Kaleye yakın pozisyon alarak ceza sahası içindeki şutlara karşı kaleyi korur; önceliği savunma arkasına çıkmak yerine çizgi üzerindeki tehditleri karşılamaktır.",
                "metrics": [
                    "Goals Conceded",
                    "Blocked Shots",
                    "Rating",
                    "Error Lead To Goal",
                    "Error Lead To Shot",
                    "Own Goals",
                    "Match Count",
                    "Minutes Played",
                ],
            },
            {
                "name": "Box-Commanding Goalkeeper",
                "description": "Ortalar, duran toplar ve hava toplarında ceza sahasına müdahale eder; hava toplarında fiziksel üstünlük kurarak savunmanın üzerindeki baskıyı azaltır.",
                "metrics": [
                    "Aerials",
                    "Aerials Won",
                    "Aerials Won (%)",
                    "Clearances",
                    "Clearance Offline",
                    "Ball Recovery",
                    "Total Duels",
                    "Duels Won",
                    "Duels Won (%)",
                ],
            },
            {
                "name": "Last-Action Goalkeeper",
                "description": "Savunma hattı geçildiğinde son müdahaleyi yapan oyuncudur; kritik pozisyonlarda topu uzaklaştırır ve doğrudan gole veya şuta yol açabilecek hataları sınırlamaya çalışır.",
                "metrics": [
                    "Last Man Tackle",
                    "Clearances",
                    "Clearance Offline",
                    "Ball Recovery",
                    "Goals Conceded",
                    "Error Lead To Goal",
                    "Error Lead To Shot",
                    "Fouls",
                    "Penalties Committed",
                ],
            },
        ],
    },
    "CF": {
        "Build-up": [
            {"name": "Defense-Pinning Forward", "description": "Rakip stoperleri geriye iterek takımın ilk bölgeden çıkmasına alan sağlar.", "metrics": ["Offsides", "Shots Total", "Touches", "Fouls Drawn", "Total Duels", "Aerials"]},
            {"name": "Depth-Running Forward", "description": "İlk bölgeden atılacak dikey paslar için savunma arkasına koşu tehdidi sunar.", "metrics": ["Offsides", "Shots Total", "Shots On Target", "Expected Goals", "Touches", "Fouls Drawn"]},
            {"name": "Long-Ball Target", "description": "Uzun paslarda hedef olur; hava ve fiziksel mücadelelerle ikinci top fırsatı sağlar.", "metrics": ["Aerials", "Aerials Won", "Aerials Won (%)", "Total Duels", "Duels Won", "Duels Won (%)", "Long Balls Won", "Fouls Drawn", "Possession Lost"]},
        ],
        "Progression": [
            {"name": "Depth-Running Forward", "description": "İkinci bölgede savunma arkasına koşularla dikey pas seçeneği oluşturur.", "metrics": ["Offsides", "Shots Total", "Shots On Target", "Expected Goals", "Touches", "Fouls Drawn"]},
            {"name": "Ball-Securing Forward", "description": "Sırtı dönük oyunda topu koruyarak takım arkadaşlarının hücuma katılmasını sağlar.", "metrics": ["Total Duels", "Duels Won", "Duels Won (%)", "Dispossessed", "Possession Lost", "Fouls Drawn", "Accurate Passes (%)"]},
            {"name": "Link Forward", "description": "İkinci bölgede topu kanatlara veya hücum orta sahasına aktararak hücum bağlantısını kurar.", "metrics": ["Passes", "Accurate Passes", "Accurate Passes (%)", "Key Passes", "Assists", "Touches", "Turn Over"]},
        ],
        "Final Third": [
            {"name": "Finishing Forward", "description": "Ceza sahasında şut kalitesi, gol üretimi ve bitiricilik verimliliğiyle öne çıkar.", "metrics": ["Goals", "Expected Goals", "Expected Goals On Target", "Shooting Performance", "Goal Conversion (%)", "On-Target to Goal Conversion (%)", "Shots On Target (%)", "Shot Quality (%)", "Aerials Won", "Aerials Won (%)", "Big Chances Missed"]},
            {"name": "Service Forward", "description": "Sonlandırıcı rolün yanında takım arkadaşlarına pozisyon hazırlayan bağlantı oyuncusu olur.", "metrics": ["Assists", "Key Passes", "Assist Efficiency (%)", "Big Chances Created", "Accurate Passes (%)", "Through Balls", "Through Balls Won", "Aerials Won", "Duels Won", "Fouls Drawn"]},
        ],
        "High Block": [
            {"name": "Front-Line Presser", "description": "Rakip stoper ve kaleciye direkt baskı uygular.", "metrics": ["Tackles", "Tackles Won", "Tackles Won (%)", "Interceptions", "Ball Recovery", "Fouls", "Total Duels"]},
            {"name": "Passing-Lane Blocker", "description": "Rakibin merkez ve stoper bağlantılarını pas açılarını kapatarak sınırlar.", "metrics": ["Interceptions", "Ball Recovery", "Touches", "Total Duels", "Duels Won", "Tackles"]},
        ],
        "Mid Block": [
            {"name": "Holding-Midfield Blocker", "description": "Rakibin stoper ile ön libero arasındaki pas bağlantısını kapatır.", "metrics": ["Interceptions", "Ball Recovery", "Touches", "Tackles", "Total Duels", "Duels Won"]},
            {"name": "Transition Threat", "description": "Takım topu kazandığında hızlı hücum için ileri pozisyonunu korur.", "metrics": ["Offsides", "Shots Total", "Fouls Drawn", "Touches", "Dispossessed", "Possession Lost"]},
            {"name": "Physical Front-Line Defender", "description": "Merkezde fiziksel mücadeleyle rakibin rahat ilerlemesini engeller.", "metrics": ["Total Duels", "Duels Won", "Duels Won (%)", "Aerials Won", "Fouls", "Fouls Drawn"]},
        ],
        "Low Block": [
            {"name": "Counter-Outlet Forward", "description": "Takım savunmadan çıktığında ilk hedef oyuncu olur.", "metrics": ["Aerials Won", "Duels Won", "Fouls Drawn", "Accurate Passes (%)", "Possession Lost", "Touches"]},
            {"name": "Set-Piece Defender", "description": "Ceza sahası savunmasında hava toplarına ve uzaklaştırmalara destek verir.", "metrics": ["Aerials", "Aerials Won", "Aerials Won (%)", "Clearances", "Blocked Shots", "Ball Recovery"]},
            {"name": "Hold-Up Outlet Forward", "description": "Top kazanıldığında ilk pası veya uzun topu koruyarak takımın bloktan çıkmasını sağlar.", "metrics": ["Passes", "Accurate Passes", "Accurate Passes (%)", "Total Duels", "Fouls Drawn", "Dispossessed", "Turn Over"]},
        ],
    },
    "WIDE": {
        "Build-up": [
            {"name": "Width-Holding Winger", "description": "Çizgi genişliğini koruyarak ilk bölgeden pasla çıkışa yardım eder.", "metrics": ["Touches", "Passes", "Accurate Passes", "Accurate Passes (%)", "Total Crosses", "Backward Passes"]},
            {"name": "Link-Up Winger", "description": "Bek ve merkez oyuncularıyla güvenli pas bağlantıları oluşturur.", "metrics": ["Passes", "Accurate Passes", "Accurate Passes (%)", "Backward Passes", "Touches", "Possession Lost", "Turn Over"]},
        ],
        "Progression": [
            {"name": "One-v-One Winger", "description": "Rakip oyuncuları dripling ile geçerek savunma dengesini bozar.", "metrics": ["Dribble Attempts", "Successful Dribbles", "Dribble Accuracy (%)", "Fouls Drawn", "Dispossessed", "Possession Lost"]},
            {"name": "Progressive Winger", "description": "Topu pas veya dripling yoluyla final bölgesine taşır.", "metrics": ["Passes In Final Third", "Successful Dribbles", "Accurate Passes", "Accurate Passes (%)", "Touches", "Possession Lost"]},
            {"name": "Combination Winger", "description": "Bek, orta saha ve hücum oyuncularıyla kısa pas bağlantıları kurar.", "metrics": ["Passes", "Accurate Passes", "Accurate Passes (%)", "Key Passes", "Through Balls", "Through Balls Won", "Assists"]},
        ],
        "Final Third": [
            {"name": "Touchline Creator", "description": "Kanattan orta ve servis üretimine odaklanır.", "metrics": ["Total Crosses", "Accurate Crosses", "Successful Crosses (%)", "Assists", "Big Chances Created", "Assist Efficiency (%)"]},
            {"name": "Inside Forward", "description": "İçe kat ederek şut ve gol tehdidi oluşturur.", "metrics": ["Shots Total", "Shots On Target", "Shots On Target (%)", "Expected Goals", "Shooting Performance", "Goal Conversion (%)", "Goals"]},
            {"name": "Creative Winger", "description": "Son pas, kilit pas ve büyük şans yaratımıyla öne çıkar.", "metrics": ["Key Passes", "Assists", "Assist Efficiency (%)", "Big Chances Created", "Through Balls", "Through Balls Won", "Passes In Final Third"]},
        ],
        "High Block": [
            {"name": "Full-Back Presser", "description": "Rakip beke doğrudan baskı uygular.", "metrics": ["Tackles", "Tackles Won", "Tackles Won (%)", "Interceptions", "Ball Recovery", "Fouls"]},
            {"name": "Wide Passing-Lane Blocker", "description": "Rakibin bek ve kanat arasındaki pas bağlantısını sınırlar.", "metrics": ["Interceptions", "Ball Recovery", "Touches", "Total Duels", "Duels Won", "Tackles"]},
            {"name": "High Ball-Winning Winger", "description": "Rakip yarı sahada top kazanıp hızlı hücum üretir.", "metrics": ["Ball Recovery", "Interceptions", "Key Passes", "Assists", "Shots Total", "Big Chances Created"]},
        ],
        "Mid Block": [
            {"name": "Tracking Winger", "description": "Rakip bek ve kanat koşularını takip ederek orta blok bütünlüğünü korur.", "metrics": ["Tackles", "Interceptions", "Ball Recovery", "Total Duels", "Duels Won", "Dribbled Past", "Clearances"]},
            {"name": "Channel Defender", "description": "Kanat koridorunu kapatıp rakibin çizgiden ilerlemesini sınırlar.", "metrics": ["Tackles Won", "Interceptions", "Blocked Shots", "Clearances", "Dribbled Past", "Ball Recovery"]},
            {"name": "Transition Winger", "description": "Top kazanıldığında hızlı şekilde hücuma çıkar.", "metrics": ["Dribble Attempts", "Successful Dribbles", "Passes In Final Third", "Key Passes", "Fouls Drawn", "Possession Lost"]},
        ],
        "Low Block": [
            {"name": "Full-Back Supporter", "description": "Kendi bekine ikili savunmada destek verir.", "metrics": ["Tackles", "Tackles Won", "Interceptions", "Total Duels", "Duels Won", "Dribbled Past"]},
            {"name": "Back-Post Defender", "description": "Ters kanattan gelen ortalarda arka direği savunur.", "metrics": ["Aerials", "Aerials Won", "Clearances", "Blocked Shots", "Ball Recovery"]},
            {"name": "Counter Carrier", "description": "Top kazanıldığında dripling veya ileri pasla kontrayı başlatır.", "metrics": ["Successful Dribbles", "Dribble Accuracy (%)", "Accurate Passes (%)", "Passes In Final Third", "Fouls Drawn", "Dispossessed"]},
        ],
    },
    "CAM": {
        "Build-up": [
            {"name": "Between-Lines Connector", "description": "Savunma ve orta saha hatları arasında pas bağlantısı oluşturur.", "metrics": ["Touches", "Passes", "Accurate Passes", "Accurate Passes (%)", "Backward Passes", "Turn Over", "Possession Lost"]},
            {"name": "Drifting Playmaker", "description": "Kanada açılarak genişlik sağlar ve ilk baskı hattından çıkışa yardım eder.", "metrics": ["Long Balls Won", "Aerials", "Aerials Won", "Aerials Won (%)", "Passes", "Accurate Passes", "Accurate Passes (%)", "Touches", "Fouls Drawn"]},
        ],
        "Progression": [
            {"name": "Between-Lines Playmaker", "description": "Rakip orta saha ve savunma hattı arasında top alır.", "metrics": ["Touches", "Passes In Final Third", "Key Passes", "Fouls Drawn", "Dispossessed", "Accurate Passes (%)"]},
            {"name": "Through-Ball Specialist", "description": "Savunma arkasına ve dar kanallara etkili paslar verir.", "metrics": ["Through Balls", "Through Balls Won", "Key Passes", "Big Chances Created", "Assists", "Assist Efficiency (%)"]},
            {"name": "Central Dribbler", "description": "Top sürerek rakip orta saha hattını aşar.", "metrics": ["Dribble Attempts", "Successful Dribbles", "Dribble Accuracy (%)", "Fouls Drawn", "Dispossessed", "Possession Lost"]},
        ],
        "Final Third": [
            {"name": "Final-Pass Creator", "description": "Kilit pas ve asist üretimiyle hücumu tamamlar.", "metrics": ["Assists", "Key Passes", "Big Chances Created", "Assist Efficiency (%)", "Through Balls Won", "Passes In Final Third"]},
            {"name": "Second Forward", "description": "Ceza sahasına koşular yaparak gol tehdidi oluşturur.", "metrics": ["Goals", "Expected Goals", "Shots Total", "Shots On Target", "Goal Conversion (%)", "Shot Quality (%)"]},
            {"name": "Shooting Threat", "description": "Ceza sahası çevresinden düzenli şut üretir.", "metrics": ["Shots Total", "Shots On Target", "Shots On Target (%)", "Shooting Performance", "Shot Quality (%)", "Goals"]},
        ],
        "High Block": [
            {"name": "Pivot-and-Center-Back Presser", "description": "Rakip ön libero veya stoperlere baskı yaparak merkezden oyun kurmayı bozar.", "metrics": ["Interceptions", "Tackles", "Tackles Won", "Ball Recovery", "Fouls", "Total Duels"]},
            {"name": "Second-Ball Collector", "description": "Ön alan baskısının arkasındaki seken topları kazanır.", "metrics": ["Ball Recovery", "Interceptions", "Total Duels", "Duels Won", "Touches"]},
            {"name": "Ball-Winning Creator", "description": "Önde kazanılan toplardan hızlı şekilde fırsat yaratır.", "metrics": ["Key Passes", "Big Chances Created", "Assists", "Shots Total", "Goals", "Ball Recovery"]},
        ],
        "Mid Block": [
            {"name": "Central Passing-Lane Blocker", "description": "Rakibin merkezden hat kıran paslarını sınırlar.", "metrics": ["Interceptions", "Ball Recovery", "Touches", "Total Duels", "Duels Won"]},
            {"name": "Press Trigger", "description": "Hatalı pas veya geri paslarda baskıyı başlatır.", "metrics": ["Tackles", "Tackles Won", "Interceptions", "Ball Recovery", "Fouls"]},
            {"name": "Transition Playmaker", "description": "Top kazanıldığında hücumcuları hızlı şekilde oyuna sokar.", "metrics": ["Key Passes", "Through Balls", "Through Balls Won", "Passes In Final Third", "Assists"]},
        ],
        "Low Block": [
            {"name": "Box-Edge Defender", "description": "Merkezde şut ve ara pas alanlarını kapatır.", "metrics": ["Interceptions", "Tackles", "Blocked Shots", "Ball Recovery", "Total Duels"]},
            {"name": "Second-Ball Playmaker", "description": "Savunmadan uzaklaştırılan topları kazanıp oyunu yeniden başlatır.", "metrics": ["Ball Recovery", "Interceptions", "Touches", "Duels Won", "Accurate Passes (%)", "Possession Lost"]},
            {"name": "Counter Initiator", "description": "Top kazanıldığında ilk yaratıcı pası veya driplingi yapar.", "metrics": ["Accurate Passes (%)", "Key Passes", "Through Balls Won", "Successful Dribbles", "Fouls Drawn", "Turn Over"]},
        ],
    },
    "CM": {
        "Build-up": [
            {"name": "First-Pass Player", "description": "Stoperlerden top alarak takımın oyun kurulumunu başlatır.", "metrics": ["Touches", "Passes", "Accurate Passes", "Accurate Passes (%)", "Backward Passes", "Possession Lost"]},
            {"name": "Press-Breaking Passer", "description": "Baskı hattının arkasına pas atarak oyunu ilerletir.", "metrics": ["Accurate Passes", "Accurate Passes (%)", "Long Balls", "Long Balls Won", "Through Balls Won", "Turn Over"]},
            {"name": "Central Ball Carrier", "description": "Dripling ile topu birinci bölgeden ikinci bölgeye taşır.", "metrics": ["Dribble Attempts", "Successful Dribbles", "Dribble Accuracy (%)", "Dispossessed", "Fouls Drawn", "Possession Lost"]},
        ],
        "Progression": [
            {"name": "Vertical Passer", "description": "Merkezden final bölgesine hat kıran paslar verir.", "metrics": ["Passes In Final Third", "Key Passes", "Through Balls", "Through Balls Won", "Accurate Passes (%)"]},
            {"name": "Play Director", "description": "Uzun paslarla takımın hücum yönünü değiştirir.", "metrics": ["Long Balls", "Long Balls Won", "Long Balls Won (%)", "Passes", "Accurate Passes"]},
            {"name": "Central Ball Carrier", "description": "Top sürerek rakip orta saha hattını geçer.", "metrics": ["Successful Dribbles", "Dribble Accuracy (%)", "Fouls Drawn", "Dispossessed", "Possession Lost"]},
        ],
        "Final Third": [
            {"name": "Front-Line Connector", "description": "Ceza sahası çevresinde pas dolaşımını sürdürür.", "metrics": ["Passes In Final Third", "Accurate Passes", "Accurate Passes (%)", "Key Passes", "Touches"]},
            {"name": "Box-Arriving Midfielder", "description": "İkinci dalga koşularıyla gol pozisyonuna girer.", "metrics": ["Goals", "Expected Goals", "Shots Total", "Shots On Target", "Goal Conversion (%)", "Shot Quality (%)"]},
            {"name": "Attack-Sustaining Player", "description": "İkinci topları kazanarak hücumu yeniden başlatır.", "metrics": ["Ball Recovery", "Interceptions", "Shots Total", "Passes In Final Third", "Possession Lost"]},
        ],
        "High Block": [
            {"name": "Pivot-and-Center-Back Presser", "description": "Rakibin ön libero veya stoperlerine öne çıkarak baskı yapar.", "metrics": ["Tackles", "Tackles Won", "Interceptions", "Ball Recovery", "Total Duels", "Fouls"]},
            {"name": "Second-Ball Winner", "description": "Pres sonrası seken topları kazanır.", "metrics": ["Ball Recovery", "Duels Won", "Aerials Won", "Interceptions", "Touches"]},
            {"name": "Counter-Presser", "description": "Top kaybından sonra ilk baskıyı yapar.", "metrics": ["Tackles", "Tackles Won", "Ball Recovery", "Fouls", "Dribbled Past"]},
        ],
        "Mid Block": [
            {"name": "Passing-Lane Blocker", "description": "Rakibin merkez ve hatlar arasındaki pas bağlantılarını kapatarak oyunun yönünü belirler. Doğrudan topa gitmekten çok doğru pozisyon alarak rakibin ilerlemesini zorlaştırır.", "metrics": ["Interceptions", "Ball Recovery", "Touches", "Tackles", "Accurate Passes (%)"]},
            {"name": "Space Protector", "description": "Takımın savunma blok bütünlüğünü korur. Boş alanları kapatır, rakibin merkezden ilerlemesini zorlaştırır ve savunma hattını destekler.", "metrics": ["Interceptions", "Ball Recovery", "Tackles", "Blocked Shots", "Clearances", "Dribbled Past"]},
            {"name": "Duel Specialist", "description": "Rakip oyuncularla bire bir mücadelelere girmeyi tercih eder. Fiziksel üstünlüğüyle top kazanır ve rakibin hücum devamlılığını bozar.", "metrics": ["Total Duels", "Duels Won", "Duels Won (%)", "Tackles", "Tackles Won", "Tackles Won (%)", "Aerials Won", "Aerials Won (%)", "Fouls"]},
        ],
        "Low Block": [
            {"name": "Box-Edge Protector", "description": "Ceza sahası önündeki merkez alanı savunur.", "metrics": ["Interceptions", "Tackles", "Blocked Shots", "Ball Recovery", "Total Duels"]},
            {"name": "Second-Ball Winner", "description": "Uzaklaştırılan topların tekrar rakibe geçmesini önler.", "metrics": ["Ball Recovery", "Aerials Won", "Duels Won", "Clearances", "Touches"]},
            {"name": "Transition Stopper", "description": "Ceza sahası çevresinde top kazanıldıktan sonra rakibin ikinci hücumunu engeller, ikinci baskıyı kırar ve savunma dengesini korur.", "metrics": ["Interceptions", "Ball Recovery", "Tackles", "Duels Won", "Total Duels", "Clearances"]},
        ],
    },
    "CDM": {
        "Build-up": [
            {"name": "Defensive Link Player", "description": "Stoperlerle orta saha arasında pas istasyonu olur.", "metrics": ["Touches", "Passes", "Accurate Passes", "Accurate Passes (%)", "Backward Passes", "Possession Lost"]},
            {"name": "First-Pass Specialist", "description": "Baskı altında oyunu güvenli ve doğru paslarla başlatır.", "metrics": ["Accurate Passes (%)", "Long Balls", "Long Balls Won", "Long Balls Won (%)", "Turn Over", "Error Lead To Shot"]},
            {"name": "Switch-of-Play Player", "description": "Uzun paslarla baskıyı ters kanada taşır.", "metrics": ["Long Balls", "Long Balls Won", "Long Balls Won (%)", "Passes", "Accurate Passes"]},
        ],
        "Progression": [
            {"name": "Vertical Playmaker", "description": "Merkezden hücum hattına dikey paslar verir.", "metrics": ["Passes In Final Third", "Through Balls", "Through Balls Won", "Accurate Passes (%)", "Key Passes"]},
            {"name": "Tempo Controller", "description": "Pas hacmi ve top kullanımıyla oyunun ritmini belirler.", "metrics": ["Passes", "Accurate Passes", "Accurate Passes (%)", "Touches", "Possession Lost"]},
            {"name": "Balance Player", "description": "Takım ilerlerken top kaybına karşı savunma güvenliği sağlar.", "metrics": ["Interceptions", "Ball Recovery", "Tackles", "Total Duels", "Duels Won"]},
        ],
        "Final Third": [
            {"name": "Rest-Defense Player", "description": "Takım hücum ederken kontra hücumlara karşı pozisyon alır.", "metrics": ["Interceptions", "Ball Recovery", "Tackles", "Tackles Won", "Fouls"]},
            {"name": "Scoring Final-Third Contributor", "description": "Final bölgede hücuma katılarak gol ve asist katkısı üretir.", "metrics": ["Goals", "Assists", "Expected Goals", "Shots Total", "Shots On Target", "Shots On Target (%)", "Goal Conversion (%)", "Key Passes", "Big Chances Created", "Assist Efficiency (%)"]},
            {"name": "Attack Redirector", "description": "Geri dönen toplarla hücum yönünü yeniden kurar.", "metrics": ["Passes", "Accurate Passes", "Accurate Passes (%)", "Long Balls Won", "Passes In Final Third"]},
        ],
        "High Block": [
            {"name": "Press-Cover Player", "description": "Ön alan baskısının arkasındaki boşluğu korur.", "metrics": ["Ball Recovery", "Interceptions", "Aerials Won", "Duels Won", "Touches"]},
            {"name": "Central Presser", "description": "Rakip orta saha oyuncusuna baskı uygular.", "metrics": ["Tackles", "Tackles Won", "Interceptions", "Total Duels", "Fouls"]},
            {"name": "Counter Breaker", "description": "Rakibin geçiş hücumunu ilk aşamada durdurur.", "metrics": ["Tackles", "Tackles Won", "Ball Recovery", "Fouls", "Yellow Cards", "Dribbled Past"]},
        ],
        "Mid Block": [
            {"name": "Passing-Lane Blocker", "description": "Rakibin merkez ve hatlar arasındaki pas bağlantılarını kapatarak oyunun yönünü belirler. Doğrudan topa gitmekten çok doğru pozisyon alarak rakibin ilerlemesini zorlaştırır.", "metrics": ["Interceptions", "Ball Recovery", "Touches", "Tackles", "Accurate Passes (%)"]},
            {"name": "Space Protector", "description": "Takımın savunma blok bütünlüğünü korur. Boş alanları kapatır, rakibin merkezden ilerlemesini zorlaştırır ve savunma hattını destekler.", "metrics": ["Interceptions", "Ball Recovery", "Tackles", "Blocked Shots", "Clearances", "Dribbled Past"]},
            {"name": "Duel Specialist", "description": "Rakip oyuncularla bire bir mücadelelere girmeyi tercih eder. Fiziksel üstünlüğüyle top kazanır ve rakibin hücum devamlılığını bozar.", "metrics": ["Total Duels", "Duels Won", "Duels Won (%)", "Tackles", "Tackles Won", "Tackles Won (%)", "Aerials Won", "Aerials Won (%)", "Fouls"]},
        ],
        "Low Block": [
            {"name": "Box-Edge Sweeper", "description": "Ceza sahası önündeki şut ve pas alanlarını kapatır.", "metrics": ["Interceptions", "Tackles", "Blocked Shots", "Ball Recovery", "Clearances"]},
            {"name": "Box Second-Ball Controller", "description": "Ceza sahası içinde veya çevresinde seken topları kontrol ederek savunma dengesini korur.", "metrics": ["Ball Recovery", "Aerials Won", "Duels Won", "Clearances", "Touches"]},
            {"name": "Transition Stopper", "description": "Ceza sahası çevresinde top kazanıldıktan sonra rakibin ikinci hücumunu engeller, ikinci baskıyı kırar ve savunma dengesini korur.", "metrics": ["Interceptions", "Ball Recovery", "Tackles", "Duels Won", "Total Duels", "Clearances"]},
        ],
    },
    "FB": {
        "Build-up": [
            {"name": "Wide Outlet Full-Back", "description": "Kanatta geniş pas seçeneği oluşturarak ilk bölgeden çıkışı sağlar.", "metrics": ["Touches", "Passes", "Accurate Passes", "Accurate Passes (%)", "Backward Passes"]},
            {"name": "Safe-Passing Full-Back", "description": "Baskı altında düşük riskli ve doğru paslarla oyun kurulumunu sürdürür.", "metrics": ["Accurate Passes (%)", "Possession Lost", "Turn Over", "Dispossessed", "Error Lead To Shot", "Error Lead To Goal"]},
            {"name": "Ball-Carrying Full-Back", "description": "Dripling yoluyla ilk baskı hattını aşar.", "metrics": ["Dribble Attempts", "Successful Dribbles", "Dribble Accuracy (%)", "Fouls Drawn", "Dispossessed"]},
        ],
        "Progression": [
            {"name": "Overlapping Full-Back", "description": "Kanat oyuncusunun dışından ileri koşular yapar.", "metrics": ["Touches", "Total Crosses", "Passes In Final Third", "Successful Dribbles", "Fouls Drawn"]},
            {"name": "Combination Full-Back", "description": "Kanat ve merkez oyuncularıyla pas bağlantıları kurar.", "metrics": ["Passes", "Accurate Passes", "Accurate Passes (%)", "Key Passes", "Through Balls Won"]},
            {"name": "Progressive Full-Back", "description": "Topu pas veya dripling yoluyla final bölgesine taşır.", "metrics": ["Successful Dribbles", "Dribble Accuracy (%)", "Passes In Final Third", "Accurate Passes (%)", "Possession Lost"]},
        ],
        "Final Third": [
            {"name": "Crossing Full-Back", "description": "Kanattan ceza sahasına servis üretir.", "metrics": ["Total Crosses", "Accurate Crosses", "Successful Crosses (%)", "Assists", "Big Chances Created"]},
            {"name": "Back-Pass Outlet", "description": "Çizgiye inip ceza sahası çevresine gol fırsatı hazırlayan paslar verir.", "metrics": ["Key Passes", "Assists", "Assist Efficiency (%)", "Big Chances Created", "Accurate Passes (%)"]},
            {"name": "Inverted-Channel Full-Back", "description": "Yarı koridora girerek pas bağlantısı kurar ve merkezde sayısal üstünlük oluşturur.", "metrics": ["Passes In Final Third", "Passes", "Accurate Passes", "Accurate Passes (%)", "Key Passes", "Through Balls", "Through Balls Won", "Touches", "Possession Lost"]},
        ],
        "High Block": [
            {"name": "High-Pressing Full-Back", "description": "Rakip kanat veya bek oyuncusuna önden baskı yapar.", "metrics": ["Tackles", "Tackles Won", "Tackles Won (%)", "Interceptions", "Ball Recovery"]},
            {"name": "Wide Ball-Winning Full-Back", "description": "Rakibi çizgiye sıkıştırıp topu geri kazanır.", "metrics": ["Ball Recovery", "Interceptions", "Total Duels", "Duels Won", "Fouls"]},
            {"name": "High-Line Defender", "description": "Savunma arkasına atılan topları karşılar.", "metrics": ["Interceptions", "Ball Recovery", "Clearances", "Aerials Won", "Dribbled Past"]},
        ],
        "Mid Block": [
            {"name": "Channel Defender", "description": "Rakibin kanattan ilerlemesini sınırlar.", "metrics": ["Tackles", "Tackles Won", "Interceptions", "Dribbled Past", "Total Duels", "Duels Won"]},
            {"name": "Passing-Lane Blocker", "description": "Bek, kanat ve merkez arasındaki pas bağlantılarını kapatır.", "metrics": ["Interceptions", "Ball Recovery", "Touches", "Tackles", "Clearances", "Dribbled Past"]},
            {"name": "Run Tracker", "description": "Rakip kanat oyuncusunun savunma arkasına koşularını takip eder.", "metrics": ["Interceptions", "Clearances", "Ball Recovery", "Aerials Won", "Fouls"]},
        ],
        "Low Block": [
            {"name": "Back-Post Defender", "description": "Ters kanattan gelen ortalarda arka direği korur.", "metrics": ["Aerials", "Aerials Won", "Aerials Won (%)", "Clearances", "Blocked Shots"]},
            {"name": "Box One-v-One Defender", "description": "Ceza sahası içinde rakip kanat oyuncularıyla bire bir savunma yapar.", "metrics": ["Tackles", "Tackles Won", "Total Duels", "Duels Won", "Dribbled Past", "Fouls"]},
            {"name": "Danger Clearer", "description": "Ceza sahası içindeki topları güvenli bölgeye uzaklaştırır.", "metrics": ["Clearances", "Clearance Offline", "Blocked Shots", "Ball Recovery", "Error Lead To Goal", "Own Goals"]},
        ],
    },
    "CB": {
        "Build-up": [
            {"name": "Safe Playmaking Center-Back", "description": "Savunmadan doğru ve güvenli paslarla oyunu başlatır.", "metrics": ["Passes", "Accurate Passes", "Accurate Passes (%)", "Touches", "Possession Lost", "Error Lead To Goal"]},
            {"name": "Line-Breaking Center-Back", "description": "Dikey ve uzun paslarla rakibin ilk baskı hattını aşar.", "metrics": ["Passes In Final Third", "Long Balls", "Long Balls Won", "Long Balls Won (%)", "Through Balls Won"]},
            {"name": "Ball-Carrying Center-Back", "description": "Rakip baskı yapmadığında dripling ile alan kazanır.", "metrics": ["Dribble Attempts", "Successful Dribbles", "Dribble Accuracy (%)", "Dispossessed", "Turn Over"]},
        ],
        "Progression": [
            {"name": "Vertical-Passing Center-Back", "description": "Topu doğrudan orta saha veya hücum hattına aktarır.", "metrics": ["Passes In Final Third", "Long Balls Won", "Through Balls", "Through Balls Won", "Accurate Passes (%)"]},
            {"name": "Switching Center-Back", "description": "Uzun paslarla hücum yönünü değiştirir.", "metrics": ["Long Balls", "Long Balls Won", "Long Balls Won (%)", "Passes", "Accurate Passes"]},
            {"name": "Defensive-Balance Center-Back", "description": "Takım ilerlerken geride alan ve geçiş güvenliği sağlar.", "metrics": ["Interceptions", "Ball Recovery", "Tackles", "Total Duels", "Duels Won"]},
        ],
        "Final Third": [
            {"name": "Set-Piece Threat", "description": "Korner ve serbest vuruşlarda hava topu tehdidi oluşturur.", "metrics": ["Aerials", "Aerials Won", "Aerials Won (%)", "Shots Total", "Goals"]},
            {"name": "Rest-Attack Collector", "description": "Rakibin uzaklaştırdığı topları yeniden kazanır.", "metrics": ["Ball Recovery", "Interceptions", "Aerials Won", "Touches", "Passes"]},
            {"name": "Counter Stopper", "description": "Top kaybında rakibin hızlı hücumunu erken keser.", "metrics": ["Interceptions", "Tackles", "Last Man Tackle", "Ball Recovery", "Fouls", "Yellow Cards"]},
        ],
        "High Block": [
            {"name": "High-Line Center-Back", "description": "Savunma çizgisini önde tutarak takım boyunu kısaltır.", "metrics": ["Interceptions", "Ball Recovery", "Touches", "Total Duels", "Duels Won"]},
            {"name": "Long-Ball Defender", "description": "Rakibin baskıdan çıkmak için kullandığı uzun topları karşılar.", "metrics": ["Aerials", "Aerials Won", "Aerials Won (%)", "Clearances", "Duels Won"]},
            {"name": "Depth Defender", "description": "Savunma arkasındaki koşuları ve boş alanı kontrol eder.", "metrics": ["Interceptions", "Last Man Tackle", "Clearances", "Ball Recovery", "Fouls", "Error Lead To Goal"]},
        ],
        "Mid Block": [
            {"name": "Forward Marker", "description": "Rakip santraforla fiziksel mücadele ederek top almasını engeller.", "metrics": ["Total Duels", "Duels Won", "Duels Won (%)", "Aerials Won", "Fouls", "Fouls Drawn"]},
            {"name": "Through-Ball Defender", "description": "Savunma arkasına atılan pasları keser.", "metrics": ["Interceptions", "Ball Recovery", "Clearances", "Last Man Tackle", "Error Lead To Shot"]},
            {"name": "Defensive Organizer", "description": "Pozisyonunu koruyarak savunma hattının dengesini sağlar.", "metrics": ["Interceptions", "Clearances", "Blocked Shots", "Goals Conceded", "Error Lead To Goal", "Rating"]},
        ],
        "Low Block": [
            {"name": "Box Defender", "description": "Ceza sahası içinde şut, pas ve fiziksel mücadelelere müdahale eder.", "metrics": ["Clearances", "Blocked Shots", "Interceptions", "Tackles", "Ball Recovery"]},
            {"name": "Aerial Dominator", "description": "Ortaları ve duran topları hava mücadelesiyle uzaklaştırır.", "metrics": ["Aerials", "Aerials Won", "Aerials Won (%)", "Clearances", "Clearance Offline"]},
            {"name": "Last-Action Center-Back", "description": "Kaleye giden şutlara veya son oyuncu koşularına kritik müdahaleler yapar.", "metrics": ["Last Man Tackle", "Blocked Shots", "Clearance Offline", "Clearances", "Error Lead To Goal", "Own Goals"]},
        ],
    },
}

NEGATIVE_METRIC_RANGES: Dict[str, Tuple[float, float]] = {
    "Goals Conceded": (0, 2),
    "Penalties Committed": (0, 0.15),
    "Penalties Missed": (0, 0.15),
    "Shots Off Target": (0, 2.5),
    "Big Chances Missed": (0, 1),
    "Aerials Lost": (0, 4),
    "Duels Lost": (0, 6),
    "Fouls": (0, 2),
    "Dispossessed": (0, 5),
    "Dribbled Past": (0, 2),
    "Turn Over": (0, 3),
    "Possession Lost": (0, 20),
    "Offsides": (0, 0.3),
    "Own Goals": (0, 0.2),
    "Error Lead To Goal": (0, 0.25),
    "Error Lead To Shot": (0, 0.4),
    "Yellow Cards": (0, 0.4),
    "Yellow & Red Cards": (0, 1),
    "Red Cards": (0, 0.2),
}

PHASE_METRIC_RANGES: Dict[str, Tuple[float, float]] = {
    "Blocked Shots": (0, 0.5),
    "Tackles Won": (0, 1.5),
    "Big Chances Missed": (0, 1),
    "Goals Conceded": (0, 2),
    "Long Balls Won": (0, 2),
    "Successful Crosses (%)": (0, 100),
    "Last Man Tackle": (0, 0.3),
    "Accurate Passes (%)": (0, 100),
    "Aerials Won (%)": (0, 100),
    "Fouls": (0, 2),
    "Hit Woodwork": (0, 0.15),
    "Total Duels": (0, 9),
    "Accurate Passes": (0, 50),
    "Error Lead To Goal": (0, 0.25),
    "Error Lead To Shot": (0, 0.4),
    "Key Passes": (0, 2),
    "Penalties Missed": (0, 0.15),
    "Yellow Cards": (0, 0.4),
    "Duels Won": (0, 6.5),
    "Rating": (0, 10),
    "Shots Total": (0, 2.5),
    "Shots On Target (%)": (0, 100),
    "Expected Goals": (0, 0.6),
    "Expected Goals On Target": (0, 0.4),
    "Shooting Performance": (-0.4, 0.4),
    "Shot Quality (%)": (0, 40),
    "On-Target Shot Quality (%)": (0, 100),
    "Goal Conversion (%)": (0, 40),
    "On-Target to Goal Conversion (%)": (0, 40),
    "Assist Efficiency (%)": (0, 25),
    "Dribble Accuracy (%)": (0, 75),
    "Total Crosses": (0, 3),
    "Passes": (0, 50),
    "Offsides": (0, 0.3),
    "Aerials Lost": (0, 4),
    "Penalties Committed": (0, 0.15),
    "Possession Lost": (0, 20),
    "Long Balls": (0, 4),
    "Aerials Won": (0, 4),
    "Clearances": (0, 1),
    "Man Of Match": (0, 0.15),
    "Match Count": (0, 35),
    "Ball Recovery": (0, 3),
    "Red Cards": (0, 0.2),
    "Accurate Crosses": (0, 1.5),
    "Goals": (0, 0.4),
    "Offsides Provoked": (0, 0.5),
    "Aerials": (0, 5),
    "Saves": (0, 4),
    "Touches": (0, 50),
    "Assists": (0, 0.3),
    "Minutes Played": (0, 90),
    "Dribble Attempts": (0, 2),
    "Tackles": (0, 3),
    "Turn Over": (0, 3),
    "Fouls Drawn": (0, 2),
    "Big Chances Created": (0, 0.75),
    "Long Balls Won (%)": (0, 100),
    "Penalties Scored": (0, 0.15),
    "Penalties Won": (0, 0.1),
    "Duels Lost": (0, 6),
    "Penalties Saved": (0, 0.5),
    "Saves Insidebox": (0, 4),
    "Shots Off Target": (0, 2.5),
    "Good High Claim": (0, 1.5),
    "Dispossessed": (0, 5),
    "Shots On Target": (0, 1.25),
    "Through Balls Won": (0, 0.25),
    "Duels Won (%)": (0, 100),
    "Punches": (0, 0.75),
    "Successful Dribbles": (0, 2),
    "Tackles Won (%)": (0, 100),
    "Interceptions": (0, 2),
    "Yellow & Red Cards": (0, 1),
    "Backward Passes": (0, 10),
    "Captain": (0, 0.5),
    "Own Goals": (0, 0.2),
    "Dribbled Past": (0, 2),
    "Clearance Offline": (0, 0.05),
    "Through Balls": (0, 0.5),
    "Passes In Final Third": (0, 6),
}

def _role_short(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    upper = raw.upper()
    if upper in ROLE_SHORT_TO_LONG:
        return upper
    return ROLE_LONG_TO_SHORT.get(raw.lower())

def _normalized_position_counts(value: Any) -> Dict[str, int]:
    if not isinstance(value, dict):
        return {}
    counts: Dict[str, int] = {}
    for raw_role, raw_count in value.items():
        short = _role_short(raw_role)
        if not short:
            continue
        try:
            count = int(float(raw_count))
        except (TypeError, ValueError):
            continue
        if count > 0:
            counts[short] = counts.get(short, 0) + count
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

def _is_goalkeeper_card(player_card: Dict[str, Any]) -> bool:
    counts = _normalized_position_counts(
        (player_card or {}).get("position_counts") or (player_card or {}).get("positionCounts")
    )
    raw_roles: List[Any] = list(counts.keys())
    roles_value = (player_card or {}).get("roles")
    if isinstance(roles_value, list):
        raw_roles.extend(roles_value)
    raw_roles.extend(
        [
            (player_card or {}).get("primary_position_code"),
            (player_card or {}).get("role"),
            (player_card or {}).get("position_name"),
            (player_card or {}).get("position"),
        ]
    )
    return any(_role_short(role) == "GK" for role in raw_roles)

def _top_position_roles(player_card: Dict[str, Any], limit: int = 2) -> List[str]:
    counts = _normalized_position_counts(
        (player_card or {}).get("position_counts") or (player_card or {}).get("positionCounts")
    )
    if counts:
        return list(counts.keys())[:limit]

    raw_roles: List[Any] = []
    raw_roles.extend(
        [
            (player_card or {}).get("primary_position_code"),
            (player_card or {}).get("primaryPositionCode"),
            (player_card or {}).get("position_name"),
            (player_card or {}).get("position"),
            (player_card or {}).get("role"),
        ]
    )
    roles_value = (player_card or {}).get("roles")
    if isinstance(roles_value, list):
        raw_roles.extend(roles_value)
    mapped: List[str] = []
    for role in raw_roles:
        short = _role_short(role)
        if short and short not in mapped:
            mapped.append(short)
        if len(mapped) >= limit:
            break
    return mapped

def _required_phase_names(player_card: Dict[str, Any]) -> List[str]:
    if _is_goalkeeper_card(player_card or {}):
        return GOALKEEPER_PHASES
    return OUTFIELD_PHASES

def _taxonomy_key_for_role(role: str) -> Optional[str]:
    if role == "GK":
        return "GK"
    if role in WIDE_PHASE_ROLES:
        return "WIDE"
    if role in FULLBACK_PHASE_ROLES:
        return "FB"
    if role in {"CF", "CAM", "CM", "CDM", "CB"}:
        return role
    return None

def _phase_taxonomy_roles(player_card: Dict[str, Any]) -> List[Tuple[str, float]]:
    counts = _normalized_position_counts(
        (player_card or {}).get("position_counts") or (player_card or {}).get("positionCounts")
    )
    if counts:
        ordered = list(counts.items())[:2]
        total_all = sum(counts.values()) or 0
        if len(ordered) == 1 or total_all <= 0:
            return [(ordered[0][0], 100.0)] if ordered else []
        first_role, first_count = ordered[0]
        second_role, second_count = ordered[1]
        first_pct = (first_count / total_all) * 100
        second_pct = (second_count / total_all) * 100
        selected = [(first_role, first_count)]
        if first_pct - second_pct <= 20:
            selected.append((second_role, second_count))
        selected_total = sum(count for _, count in selected) or 1
        return [(role, (count / selected_total) * 100) for role, count in selected]

    roles = _top_position_roles(player_card or {}, 2)
    return [(roles[0], 100.0)] if roles else []

def _metric_context_value(metric_docs: List[Dict[str, Any]], metric_name: str) -> Optional[Any]:
    selected: Optional[Any] = _derived_metric_value(metric_name, metric_docs)
    if selected not in (None, ""):
        return selected
    for doc in metric_docs or []:
        selected = _metric_value_from_metadata(doc.get("metadata") or {}, metric_name)
        if selected not in (None, ""):
            return selected
    return None

def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))

def _metric_signal_strength(metric_docs: List[Dict[str, Any]], metric_name: str) -> Optional[float]:
    value = _to_float(_metric_context_value(metric_docs, metric_name))
    metric_range = PHASE_METRIC_RANGES.get(metric_name) or NEGATIVE_METRIC_RANGES.get(metric_name)
    if value is None or not metric_range:
        return None

    minimum, maximum = metric_range
    if maximum <= minimum:
        return None
    normalized = _clamp((value - minimum) / (maximum - minimum))
    if metric_name in NEGATIVE_METRIC_RANGES:
        normalized = 1 - normalized
    return normalized

def _phase_category_score(category: Dict[str, Any], metric_docs: List[Dict[str, Any]]) -> float:
    strengths: List[float] = []
    for metric in category.get("metrics") or []:
        strength = _metric_signal_strength(metric_docs, metric)
        if strength is not None:
            strengths.append(strength)
    if not strengths:
        return 1.0

    strengths.sort(reverse=True)
    strongest = strengths[:5]
    average_strength = sum(strongest) / len(strongest)
    return 0.25 + average_strength

def _normalize_phase_distribution_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged_items: Dict[str, Dict[str, Any]] = {}
    for item in items:
        category_name = str(item.get("category") or "")
        if not category_name:
            continue
        existing = merged_items.get(category_name)
        if not existing:
            merged_items[category_name] = {
                **item,
                "source_roles": [item.get("source_role")],
                "metrics": list(item.get("metrics") or []),
                "available_metrics": list(item.get("available_metrics") or []),
            }
            continue

        existing["percentage"] = float(existing.get("percentage") or 0) + float(item.get("percentage") or 0)
        existing["score"] = max(float(existing.get("score") or 0), float(item.get("score") or 0))
        source_role = item.get("source_role")
        if source_role and source_role not in existing["source_roles"]:
            existing["source_roles"].append(source_role)
        for metric in item.get("metrics") or []:
            if metric not in existing["metrics"]:
                existing["metrics"].append(metric)
        for metric_value in item.get("available_metrics") or []:
            if metric_value not in existing["available_metrics"]:
                existing["available_metrics"].append(metric_value)
        existing["source_role"] = "/".join(str(role) for role in existing["source_roles"] if role)

    items = list(merged_items.values())
    total = sum(float(item["percentage"]) for item in items) or 1.0
    normalized: List[Dict[str, Any]] = []
    rounded_total = 0
    for index, item in enumerate(items):
        if index == len(items) - 1:
            percentage = max(0, 100 - rounded_total)
        else:
            percentage = int(round((float(item["percentage"]) / total) * 100))
            rounded_total += percentage
        normalized.append({**item, "percentage": percentage})
    return normalized

def _phase_taxonomy_distribution_for_role(
    role: str,
    metric_docs: List[Dict[str, Any]],
    phase: str,
) -> List[Dict[str, Any]]:
    taxonomy_key = _taxonomy_key_for_role(role)
    categories = (PHASE_ROLE_TAXONOMY.get(taxonomy_key or "") or {}).get(phase, [])
    if not categories:
        return []

    items: List[Dict[str, Any]] = []
    scores = [_phase_category_score(category, metric_docs) for category in categories]
    score_total = sum(scores) or float(len(categories)) or 1.0
    for category, score in zip(categories, scores):
        metrics = category.get("metrics") or []
        available_metrics: List[str] = []
        for metric in metrics:
            value = _metric_context_value(metric_docs, metric)
            if value not in (None, ""):
                available_metrics.append(f"{metric}={value}")
        items.append(
            {
                "source_role": role,
                "category": category["name"],
                "percentage": score / score_total,
                "score": round(score, 3),
                "description": category["description"],
                "metrics": metrics,
                "available_metrics": available_metrics[:5],
            }
        )
    return _normalize_phase_distribution_items(items)

def _phase_taxonomy_distribution_sets(
    player_card: Dict[str, Any],
    metric_docs: List[Dict[str, Any]],
    phase: str,
) -> List[Dict[str, Any]]:
    selected_roles = _phase_taxonomy_roles(player_card or {})
    sets: List[Dict[str, Any]] = []
    seen_taxonomy_keys: set[str] = set()
    for role, _role_weight in selected_roles:
        taxonomy_key = _taxonomy_key_for_role(role)
        if not taxonomy_key or taxonomy_key in seen_taxonomy_keys:
            continue
        distribution = _phase_taxonomy_distribution_for_role(role, metric_docs, phase)
        if distribution:
            sets.append({"role": role, "items": distribution})
            seen_taxonomy_keys.add(taxonomy_key)
    return sets

def _metric_key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())

def _metric_value_from_metadata(metadata: Dict[str, Any], metric_name: str) -> Optional[Any]:
    if not isinstance(metadata, dict):
        return None

    target_key = _metric_key(metric_name)
    for raw_metric, raw_value in metadata.items():
        if _metric_key(raw_metric) == target_key:
            return raw_value

    for container_key in ("stats", "statistics", "metrics"):
        raw_stats = metadata.get(container_key)
        if not isinstance(raw_stats, list):
            continue
        for stat in raw_stats:
            if not isinstance(stat, dict):
                continue
            raw_name = stat.get("metric") or stat.get("stat") or stat.get("label") or stat.get("name")
            if _metric_key(raw_name) != target_key:
                continue
            return stat.get("value") or stat.get("amount") or stat.get("score")

    return None

def _to_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, str):
            value = value.replace("%", "").strip()
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None

def _derived_metric_value(metric_name: str, metric_docs: List[Dict[str, Any]]) -> Optional[float]:
    dependencies = {
        "Shot Quality (%)": ("Expected Goals", "Shots Total"),
        "On-Target Shot Quality (%)": ("Expected Goals On Target", "Shots On Target"),
        "Goal Conversion (%)": ("Goals", "Shots Total"),
        "On-Target to Goal Conversion (%)": ("Goals", "Shots On Target"),
        "Assist Efficiency (%)": ("Assists", "Key Passes"),
        "Dribble Accuracy (%)": ("Successful Dribbles", "Dribble Attempts"),
    }
    if metric_name not in dependencies:
        return None

    numerator_metric, denominator_metric = dependencies[metric_name]
    numerator: Optional[float] = None
    denominator: Optional[float] = None
    for doc in metric_docs or []:
        metadata = doc.get("metadata") or {}
        if numerator is None:
            numerator = _to_float(_metric_value_from_metadata(metadata, numerator_metric))
        if denominator is None:
            denominator = _to_float(_metric_value_from_metadata(metadata, denominator_metric))
        if numerator is not None and denominator is not None:
            break

    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round((numerator / denominator) * 100, 2)

def with_phase_distributions(content_json):
    """Enrich new and cached reports using their original report metric snapshot."""
    if not isinstance(content_json, dict):
        return content_json
    card = dict(content_json.get("player_card") or {})
    docs = content_json.get("metrics_docs") or []
    # Older mobile report cards omitted role counts; recover them from the same
    # saved metric documents enterprise uses when constructing its player card.
    for doc in docs:
        meta = doc.get("metadata") or {}
        counts = _normalized_position_counts(meta.get("position_counts"))
        if counts and not card.get("position_counts") and not card.get("positionCounts"):
            card["position_counts"] = counts
        if not card.get("primary_position_code"):
            primary = _role_short(meta.get("primary_position_code"))
            if not primary and counts:
                primary = next(iter(counts))
            if primary:
                card["primary_position_code"] = primary
    return {**content_json, "phase_distributions": [
        {"phase": phase, "role_views": _phase_taxonomy_distribution_sets(card, docs, phase)}
        for phase in _required_phase_names(card)
    ]}
