"""Quarter and half tendencies per team, from stats.wnba.com per-period team box scores.

For every game, each team's number in a quarter (margin, total, pace) is compared with
its own average over the other three quarters of that same game, and the league's usual
pattern for that quarter is subtracted. What's left is the team's tendency. A t-test on
those per-game differences says how unusual it is; Benjamini-Hochberg keeps the share of
false alarms among the "stand out" list near FDR_Q even though hundreds of tests are run.
"""
import math
import time

from nba_api.stats.endpoints import teamgamelogs

QUARTERS = ('Q1', 'Q2', 'Q3', 'Q4')
SEGMENTS = QUARTERS + ('1H', '2H')
COMPARISONS = QUARTERS + ('1H-2H',)
STATS = ('margin', 'total', 'pace')
VENUES = ('home', 'away', 'all')
FDR_Q = 0.10
WATCH_P = 0.01


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
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab*x/qap
    d = 1/(d if abs(d) > 1e-30 else 1e-30)
    h = d
    for m in range(1, 200):
        m2 = 2*m
        aa = m*(b-m)*x/((qam+m2)*(a+m2))
        d = 1 + aa*d; d = 1/(d if abs(d) > 1e-30 else 1e-30)
        c = 1 + aa/c if abs(1 + aa/c) > 1e-30 else 1e-30
        h *= d*c
        aa = -(a+m)*(qab+m)*x/((a+m2)*(qap+m2))
        d = 1 + aa*d; d = 1/(d if abs(d) > 1e-30 else 1e-30)
        c = 1 + aa/c if abs(1 + aa/c) > 1e-30 else 1e-30
        de = d*c
        h *= de
        if abs(de - 1) < 3e-12:
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


def build(league_id, season, season_types):
    # raw[gid][team] = {'home': bool, 1: box, 2: box, ...}
    raw = {}
    for period in (1, 2, 3, 4):
        for st in season_types:
            for r in _fetch(league_id, season, st, period):
                t = raw.setdefault(str(r['GAME_ID']), {}).setdefault(r['TEAM_NAME'], {'home': ' vs. ' in r['MATCHUP']})
                t[period] = {k: float(r[K] or 0) for k, K in (('pts', 'PTS'), ('fga', 'FGA'), ('fgm', 'FGM'), ('fta', 'FTA'),
                                                               ('oreb', 'OREB'), ('dreb', 'DREB'), ('tov', 'TOV'))}

    # One record per team-game: venue plus margin/total/pace for every segment
    records = {}
    for gid, teams in raw.items():
        if len(teams) != 2 or any(p not in t for t in teams.values() for p in (1, 2, 3, 4)):
            continue
        (na, a), (nb, b) = teams.items()
        for me, my, opp in ((na, a, b), (nb, b, a)):
            seg = {}
            for p in (1, 2, 3, 4):
                poss = (_poss(my[p], opp[p]) + _poss(opp[p], my[p])) / 2
                seg['Q%d' % p] = {'pts': my[p]['pts'], 'opp': opp[p]['pts'], 'poss': poss}
            for h, qs in (('1H', ('Q1', 'Q2')), ('2H', ('Q3', 'Q4'))):
                seg[h] = {k: sum(seg[q][k] for q in qs) for k in ('pts', 'opp', 'poss')}
            row = {'venue': 'home' if my['home'] else 'away'}
            for s, v in seg.items():
                per40 = 4 if s.startswith('Q') else 2   # express pace per 40 minutes, like full-game pace
                row[s] = {'margin': v['pts'] - v['opp'], 'total': v['pts'] + v['opp'], 'pace': v['poss']*per40,
                          'pts': v['pts'], 'opp': v['opp']}
            records.setdefault(me, []).append(row)

    all_rows = [r for rows in records.values() for r in rows]
    league = {s: {k: round(_mean([r[s][k] for r in all_rows]), 1) for k in ('total', 'pace')} for s in SEGMENTS}

    def diffs(rows, comp, stat):
        if comp == '1H-2H':
            return [r['1H'][stat] - r['2H'][stat] for r in rows]
        others = [q for q in QUARTERS if q != comp]
        return [r[comp][stat] - sum(r[q][stat] for q in others)/3 for r in rows]

    def pick(rows, venue):
        return rows if venue == 'all' else [r for r in rows if r['venue'] == venue]

    baseline = {v: {c: {s: _mean(diffs(pick(all_rows, v), c, s)) for s in STATS} for c in COMPARISONS} for v in VENUES}

    teams_out, tests = {}, []
    for team, rows in sorted(records.items()):
        tout = {'games': {}, 'segments': {}, 'tendencies': {}}
        for v in VENUES:
            vr = pick(rows, v)
            tout['games'][v] = len(vr)
            tout['segments'][v] = {}
            for s in SEGMENTS:
                n = len(vr)
                tout['segments'][v][s] = None if not n else {
                    'margin': round(_mean([r[s]['margin'] for r in vr]), 1),
                    'total': round(_mean([r[s]['total'] for r in vr]), 1),
                    'pace': round(_mean([r[s]['pace'] for r in vr]), 1),
                    'pts': round(_mean([r[s]['pts'] for r in vr]), 1),
                    'opp': round(_mean([r[s]['opp'] for r in vr]), 1),
                    'win_pct': round(100*sum(1 if r[s]['margin'] > 0 else 0.5 if r[s]['margin'] == 0 else 0 for r in vr)/n),
                    'over_pct': round(100*sum(1 for r in vr if r[s]['total'] > league[s]['total'])/n),
                }
            tout['tendencies'][v] = {}
            for c in COMPARISONS:
                tout['tendencies'][v][c] = {}
                for st in STATS:
                    d = diffs(vr, c, st)
                    n = len(d)
                    if n < 5:
                        continue
                    m = _mean(d)
                    sd = math.sqrt(sum((x-m)**2 for x in d)/(n-1))
                    delta = m - baseline[v][c][st]
                    t = delta/(sd/math.sqrt(n)) if sd > 0 else 0.0
                    p = t_pvalue(t, n-1)
                    if c == '1H-2H':
                        mine, other = _mean([r['1H'][st] for r in vr]), _mean([r['2H'][st] for r in vr])
                    else:
                        mine = _mean([r[c][st] for r in vr])
                        other = _mean([sum(r[q][st] for q in QUARTERS if q != c)/3 for r in vr])
                    cell = {'delta': round(delta, 2), 'league': round(baseline[v][c][st], 2), 'mine': round(mine, 1),
                            'others': round(other, 1), 't': round(t, 2), 'p': round(p, 4), 'n': n}
                    tout['tendencies'][v][c][st] = cell
                    tests.append((p, team, v, c, st, cell))
        teams_out[team] = tout

    # Benjamini-Hochberg across every test
    tests.sort(key=lambda x: x[0])
    N = len(tests)
    cutoff = 0
    for i, x in enumerate(tests, 1):
        if x[0] <= FDR_Q*i/N:
            cutoff = i
    standouts, watch = [], []
    for i, (p, team, v, c, st, cell) in enumerate(tests, 1):
        item = dict(team=team, venue=v, comp=c, stat=st, **cell)
        if i <= cutoff:
            cell['flag'] = 'standout'
            standouts.append(item)
        elif p < WATCH_P:
            cell['flag'] = 'watch'
            watch.append(item)

    return {
        'games': len(all_rows)//2,
        'league': league,
        'baseline': {v: {c: {s: round(x, 2) for s, x in cs.items()} for c, cs in vc.items()} for v, vc in baseline.items()},
        'teams': teams_out,
        'tests': N,
        'fdr_q': FDR_Q,
        'watch_p': WATCH_P,
        'expected_watch_by_chance': round(N*WATCH_P, 1),
        'standouts': standouts,
        'watch': watch,
    }
