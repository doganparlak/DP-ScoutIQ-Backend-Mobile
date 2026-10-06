"""Reference-role weights for statistical similarity, independent of discovery preferences."""
from similar_players_module.features import CATEGORY_DEFINITIONS, CONTEXT_BUNDLES

# Category order: impact, shooting, passing, defending, security, goalkeeping.
CATEGORY_WEIGHTS = {
    'CF': (20, 45, 15, 10, 10, 0),
    'WING': (35, 20, 30, 5, 10, 0),
    'CAM': (30, 15, 40, 5, 10, 0),
    'CM': (10, 5, 45, 25, 15, 0),
    'CDM': (5, 5, 30, 40, 20, 0),
    'BACK': (15, 5, 30, 35, 15, 0),
    'CB': (3, 2, 20, 55, 20, 0),
    'GK': (0, 0, 20, 0, 10, 70),
}
GROUP_KEYS = {
    'impact': ('creation', 'dribbling', 'involvement'),
    'shooting': ('threat', 'quality', 'finishing', 'penalties'),
    'passing': ('connection', 'creative', 'long', 'crossing'),
    'defending': ('recovery', 'blocking', 'duels', 'aerial', 'offsides'),
    'security': ('retention', 'errors', 'discipline'),
    'goalkeeping': ('saves', 'claims', 'penalties'),
}
GROUP_WEIGHTS = {
    'impact': {
        'CF': (25, 30, 45), 'WING': (30, 50, 20), 'CAM': (50, 30, 20),
        'CM': (35, 20, 45), 'CDM': (15, 20, 65), 'BACK': (25, 40, 35), 'CB': (10, 10, 80),
    },
    'shooting': {
        'CF': (30, 35, 33, 2), 'WING': (40, 35, 23, 2), 'CAM': (40, 35, 23, 2),
        'CM': (50, 30, 18, 2), 'CDM': (50, 30, 18, 2), 'BACK': (50, 30, 18, 2), 'CB': (40, 30, 28, 2),
    },
    'passing': {
        'CF': (40, 45, 10, 5), 'WING': (20, 45, 5, 30), 'CAM': (25, 60, 10, 5),
        'CM': (45, 30, 20, 5), 'CDM': (55, 15, 25, 5), 'BACK': (30, 20, 15, 35),
        'CB': (60, 5, 33, 2), 'GK': (55, 0, 45, 0),
    },
    'defending': {
        'CF': (20, 5, 35, 38, 2), 'WING': (40, 5, 45, 8, 2), 'CAM': (45, 5, 40, 8, 2),
        'CM': (45, 10, 35, 8, 2), 'CDM': (45, 15, 25, 13, 2), 'BACK': (35, 20, 35, 8, 2),
        'CB': (25, 30, 20, 23, 2),
    },
    'security': {
        'CF': (75, 10, 15), 'WING': (80, 10, 10), 'CAM': (80, 10, 10),
        'CM': (70, 20, 10), 'CDM': (60, 25, 15), 'BACK': (65, 20, 15),
        'CB': (45, 40, 15), 'GK': (35, 60, 5),
    },
    'goalkeeping': {'GK': (75, 23, 2)},
}
ROLE_PROFILES = {
    **{role: role for role in ('CF', 'CAM', 'CM', 'CDM', 'CB', 'GK')},
    **{role: 'WING' for role in ('LM', 'LW', 'RM', 'RW')},
    **{role: 'BACK' for role in ('LB', 'RB', 'LWB', 'RWB')},
    'LCB': 'CB', 'RCB': 'CB', 'LCM': 'CM', 'RCM': 'CM',
    'LDM': 'CDM', 'RDM': 'CDM', 'LAM': 'CAM', 'RAM': 'CAM', 'LCF': 'CF', 'RCF': 'CF',
}


def blended_bundles(role_shares):
    """Blend absolute category × subgroup weights using eligible reference-role shares."""
    profiles = [(ROLE_PROFILES[role], share) for role, share in role_shares.items()
                if role in ROLE_PROFILES and share > 0]
    total = sum(share for _, share in profiles)
    bundles = {}
    for profile, share in profiles:
        for category, category_weight in zip(GROUP_KEYS, CATEGORY_WEIGHTS[profile]):
            if not category_weight:
                continue
            for group, group_weight in zip(GROUP_KEYS[category], GROUP_WEIGHTS[category][profile]):
                if group_weight:
                    key = f'{category}.{group}'
                    bundles[key] = bundles.get(key, 0) + share / total * category_weight / 100 * group_weight / 100
    return bundles


def feature_weights(source, distributions, role_shares):
    bundles = blended_bundles(role_shares)
    categories = []
    for category, _, _, groups in CATEGORY_DEFINITIONS:
        category_mass = sum(weight for key, weight in bundles.items() if key.startswith(f'{category}.'))
        available = []
        for key, _, _, metrics in groups:
            bundle_key = f'{category}.{key}'
            mass = bundles.get(bundle_key, 0)
            names = list(dict.fromkeys([m.lstrip('-') for m in metrics] + CONTEXT_BUNDLES.get(bundle_key, [])))
            names = [m for m in names if m in source and m in distributions]
            if mass and names:
                available.append((mass, names))
        if available:
            categories.append((category_mass, available))
    weights = {}
    total_mass = sum(mass for mass, _ in categories)
    for category_mass, available in categories:
        available_mass = sum(mass for mass, _ in available)
        for mass, names in available:
            for name in names:
                weights[name] = weights.get(name, 0) + category_mass / total_mass * mass / available_mass / len(names)
    # Missing source metrics redistribute within their group/category. Candidate
    # omissions never change these weights: they reduce weighted common coverage.
    return weights
