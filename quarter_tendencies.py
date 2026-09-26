"""Quarter and half tendencies and home-court advantage per team, from stats.wnba.com
per-period team box scores.

Tendencies: for every game, a team's number in one period is compared with another period
of the same game, and the league's usual difference is subtracted. What's left is the
team's tendency. Comparisons:
  margin  each quarter vs the team's other three quarters; 1st half vs 2nd half
  total   Q1 vs Q2 and Q3 vs Q4 only. Both quarters of a pair are played toward the same
          baskets, so the choice of which end to shoot at first (toward or away from a
          bench) cancels out. Half totals are not compared for that reason.
  pace    each quarter vs the team's other three quarters

Home-court advantage (HCA): half the gap between a team's average home margin and its
average road margin, compared with the league's, for the full game and each period.

Each comparison gets a t-test p-value; Benjamini-Hochberg across all of them marks the ones
that survive testing this many things at once.
"""
import math
import time

from nba_api.stats.endpoints import teamgamelogs

QUARTERS = ('Q1', 'Q2', 'Q3', 'Q4')
SEGMENTS = QUARTERS + ('1H', '2H')
HCA_SEGMENTS = ('Game',) + SEGMENTS
COMPARISONS = {
    'margin': ('Q1', 'Q2', 'Q3', 'Q4', '1H-2H'),
    'total': ('Q1-Q2', 'Q3-Q4'),
    'pace': ('Q1', 'Q2', 'Q3', 'Q4'),
}
VENUES = ('home', 'away', 'all')
FDR_Q = 0.10
KEEP_P = 0.10   # signals at or below this p are sent to the page, which filters further


def _fetch(league_id, season, season_type, period, retries=4, delay=10):
    for i in range(retries):
        try:
            return teamgamelogs.TeamGameLogs(
                league_id_nullable=league_id, season_nullable=season,
                season_type_nullable=season_type, period_nullable=period, timeout=60,
            ).get_normalized_dict()['TeamGameLogs']
        except Exception as e:
            if i == retries - 1:
                raise
            print("  Retry " + str(i+1) + " (" + str(e) + ") waiting " + str(delay) + "s...")
            time.sleep(delay)


def _poss(a, b):
    # Same possession estimate as the full-game pace in fetch_wnba.py
    eps = 1e-8
    return a['fga'] + 0.44*a['fta'] - 1.07*(a['oreb']/(a['oreb']+b['dreb']+eps))*(a['fga']-a['fgm']) + a['tov']


def _betacf(a, b, x):
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab*x/qap
    d = 1/(d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 200):
        m2 = 2*m
        for aa in (m*(b-m)*x/((qam+m2)*(a+m2)), -(a+m)*(qab+m)*x/((a+m2)*(qap+m2))):
            d = 1 + aa*d
            d = 1/(d if abs(d) > tiny else tiny)
            c = 1 + aa/c
            c = c if abs(c) > tiny else tiny
            h *= d*c
        if abs(d*c - 1) < 3e-12:
            break
    return h


def _betainc(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(math.lgamma(a+b) - math.lgamma(a) - math.lgamma(b) + a*math.log(x) + b*math.log(1-x))
    if x < (a+1)/(a+b+2):
        return bt*_betacf(a, b, x)/a
    return 1 - bt*_betacf(b, a, 1-x)/b


def t_pvalue(t, df):
    """Two-sided p-value for Student's t."""
    if df <= 0 or t != t:
        return 1.0
    return _betainc(df/2, 0.5, df/(df + t*t))


def _mean(v):
    return sum(v)/len(v) if v else None


def _var(v):
    m = _mean(v)
    return sum((x-m)**2 for x in v)/(len(v)-1)


def build(league_id, season, season_types):
    # raw[gid][team] = {'home': bool, 0: full game, 1..4: quarters}
    raw = {}
    for period in (0, 1, 2, 3, 4):
        for st in season_types:
            for r in _fetch(league_id, season, st, '' if period == 0 else period):
                t = raw.setdefault(str(r['GAME_ID']), {}).setdefault(r['TEAM_NAME'], {'home': ' vs. ' in r['MATCHUP']})
                t[period] = {k: float(r[K] or 0) for k, K in (('pts', 'PTS'), ('fga', 'FGA'), ('fgm', 'FGM'), ('fta', 'FTA'),
                                                               ('oreb', 'OREB'), ('dreb', 'DREB'), ('tov', 'TOV'))}

    # One record per team-game: venue plus margin/total/pace for every segment
    records = {}
    for gid, teams in raw.items():
        if len(teams) != 2 or any(p not in t for t in teams.values() for p in (0, 1, 2, 3, 4)):
            continue
        (na, a), (nb, b) = teams.items()
        if a['home'] == b['home']:
            continue
        for me, my, opp in ((na, a, b), (nb, b, a)):
            seg = {}
            for p in (1, 2, 3, 4):
                poss = (_poss(my[p], opp[p]) + _poss(opp[p], my[p])) / 2
                seg['Q%d' % p] = {'pts': my[p]['pts'], 'opp': opp[p]['pts'], 'poss': poss}
            for h, qs in (('1H', ('Q1', 'Q2')), ('2H', ('Q3', 'Q4'))):
                seg[h] = {k: sum(seg[q][k] for q in qs) for k in ('pts', 'opp', 'poss')}
            row = {'venue': 'home' if my['home'] else 'away',
                   'Game': {'margin': my[0]['pts'] - opp[0]['pts']}}
            for s, v in seg.items():
                per40 = 4 if s.startswith('Q') else 2   # express pace per 40 minutes, like full-game pace
                row[s] = {'margin': v['pts'] - v['opp'], 'total': v['pts'] + v['opp'], 'pace': v['poss']*per40,
                          'pts': v['pts'], 'opp': v['opp']}
            records.setdefault(me, []).append(row)

    all_rows = [r for rows in records.values() for r in rows]
    league = {s: {k: round(_mean([r[s][k] for r in all_rows]), 1) for k in ('total', 'pace')} for s in SEGMENTS}

    def diffs(rows, comp, stat):
        if '-' in comp:
            x, y = comp.split('-')
            x, y = ('1H', '2H') if x == '1H' else (x, y)
            return [r[x][stat] - r[y][stat] for r in rows]
        others = [q for q in QUARTERS if q != comp]
        return [r[comp][stat] - sum(r[q][stat] for q in others)/3 for r in rows]

    def sides(rows, comp, stat):
        if '-' in comp:
            x, y = comp.split('-')
            x, y = ('1H', '2H') if x == '1H' else (x, y)
            return _mean([r[x][stat] for r in rows]), _mean([r[y][stat] for r in rows])
        return (_mean([r[comp][stat] for r in rows]),
                _mean([sum(r[q][stat] for q in QUARTERS if q != comp)/3 for r in rows]))

    def pick(rows, venue):
        return rows if venue == 'all' else [r for r in rows if r['venue'] == venue]

    baseline = {v: {st: {c: _mean(diffs(pick(all_rows, v), c, st)) for c in cs} for st, cs in COMPARISONS.items()}
                for v in VENUES}

    tests = []   # (p, kind, team, cell) - cell dicts are shared with the per-team output
    teams_out = {}
    for team, rows in sorted(records.items()):
        tout = {'games': {}, 'segments': {}, 'tendencies': {}, 'hca': {}}
        for v in VENUES:
            vr = pick(rows, v)
            n = len(vr)
            tout['games'][v] = n
            tout['segments'][v] = {s: None if not n else {
                'margin': round(_mean([r[s]['margin'] for r in vr]), 1),
                'total': round(_mean([r[s]['total'] for r in vr]), 1),
                'pace': round(_mean([r[s]['pace'] for r in vr]), 1),
                'win_pct': round(100*sum(1 if r[s]['margin'] > 0 else 0.5 if r[s]['margin'] == 0 else 0 for r in vr)/n),
                'over_pct': round(100*sum(1 for r in vr if r[s]['total'] > league[s]['total'])/n),
            } for s in SEGMENTS}
            tout['tendencies'][v] = {}
            for st, comps in COMPARISONS.items():
                tout['tendencies'][v][st] = {}
                for c in comps:
                    d = diffs(vr, c, st)
                    if len(d) < 5:
                        continue
                    sd = math.sqrt(_var(d))
                    delta = _mean(d) - baseline[v][st][c]
                    t = delta/(sd/math.sqrt(len(d))) if sd > 0 else 0.0
                    p = t_pvalue(t, len(d)-1)
                    mine, other = sides(vr, c, st)
                    cell = {'venue': v, 'comp': c, 'stat': st, 'delta': round(delta, 2), 'league': round(baseline[v][st][c], 2),
                            'mine': round(mine, 1), 'others': round(other, 1), 'p': round(p, 4), 'n': len(d)}
                    tout['tendencies'][v][st][c] = cell
                    tests.append((p, 'tendency', team, cell))

        # Home-court advantage: half of (home margin - road margin), Welch t-test against the league's
        home, away = pick(rows, 'home'), pick(rows, 'away')
        for s in HCA_SEGMENTS:
            hm, am = [r[s]['margin'] for r in home], [r[s]['margin'] for r in away]
            if len(hm) < 5 or len(am) < 5:
                continue
            lg_hca = _mean([r[s]['margin'] for r in all_rows if r['venue'] == 'home'])
            hca = (_mean(hm) - _mean(am)) / 2
            vh, va = _var(hm)/len(hm), _var(am)/len(am)
            se = math.sqrt(vh + va) / 2
            df = (vh + va)**2 / (vh**2/(len(hm)-1) + va**2/(len(am)-1)) if vh + va > 0 else 1
            t = (hca - lg_hca)/se if se > 0 else 0.0
            p = t_pvalue(t, df)
            cell = {'seg': s, 'home_margin': round(_mean(hm), 1), 'road_margin': round(_mean(am), 1), 'hca': round(hca, 2),
                    'league': round(lg_hca, 2), 'delta': round(hca - lg_hca, 2), 'p': round(p, 4),
                    'n_home': len(hm), 'n_road': len(am)}
            tout['hca'][s] = cell
            tests.append((p, 'hca', team, cell))
        teams_out[team] = tout

    # Benjamini-Hochberg across every test
    tests.sort(key=lambda x: x[0])
    N = len(tests)
    cutoff = 0
    for i, x in enumerate(tests, 1):
        if x[0] <= FDR_Q*i/N:
            cutoff = i
    signals = []
    for i, (p, kind, team, cell) in enumerate(tests, 1):
        if i <= cutoff:
            cell['standout'] = True
        if p <= KEEP_P:
            signals.append(dict(kind=kind, team=team, **cell))

    return {
        'games': len(all_rows)//2,
        'league': league,
        'teams': teams_out,
        'tests': N,
        'fdr_q': FDR_Q,
        'keep_p': KEEP_P,
        'signals': signals,
    }
