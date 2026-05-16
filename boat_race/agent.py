import json
import math
from .models import AgentWeight, RacePrediction

COURSE_ADVANTAGE = {1: 1.00, 2: 0.35, 3: 0.28, 4: 0.25, 5: 0.22, 6: 0.20}
LEARNING_RATE = 0.02
MIN_W, MAX_W = 0.03, 0.70

FEATURE_KEYS = ('course', 'player_rate', 'local_rate', 'motor_rate', 'tenji_time', 'tenji_st')


def _clamp(v):
    return max(MIN_W, min(MAX_W, v))


def _normalize_weights(w):
    total = (w.w_course + w.w_player_rate + w.w_local_rate +
             w.w_motor_rate + w.w_tenji_time + w.w_tenji_st)
    if total == 0:
        return
    w.w_course      /= total
    w.w_player_rate /= total
    w.w_local_rate  /= total
    w.w_motor_rate  /= total
    w.w_tenji_time  /= total
    w.w_tenji_st    /= total


class PredictionAgent:
    def __init__(self):
        self.weights = self._load_or_create()

    def _load_or_create(self):
        w = AgentWeight.objects.order_by('-updated_at').first()
        if not w:
            w = AgentWeight.objects.create()
        return w

    def _extract_features(self, boats, before_info=None):
        features = {}
        before_boats = (before_info or {}).get('boats', {})
        for boat in boats:
            n = boat['number']
            try:
                pr = min(float(boat.get('win_rate', '3.0')) / 10.0, 1.0)
            except Exception:
                pr = 0.3
            try:
                lr = min(float(boat.get('local_rate', str(boat.get('win_rate', '3.0')))) / 10.0, 1.0)
            except Exception:
                lr = pr
            try:
                mr = min(float(boat.get('motor_rate', '30.0')) / 100.0, 1.0)
            except Exception:
                mr = 0.3
            bi = before_boats.get(n, {})
            try:
                tt = float(bi.get('tenji_time', '7.0'))
                tenji_time_score = max(0.0, min(1.0, (7.5 - tt) / 1.1))
            except Exception:
                tenji_time_score = 0.5
            try:
                st_raw = bi.get('tenji_st', '')
                st_val = int(''.join(filter(str.isdigit, str(st_raw)))) if st_raw else 15
                tenji_st_score = max(0.0, min(1.0, (30 - st_val) / 30.0))
            except Exception:
                tenji_st_score = 0.5
            features[n] = {
                'course':      COURSE_ADVANTAGE.get(n, 0.20),
                'player_rate': pr,
                'local_rate':  lr,
                'motor_rate':  mr,
                'tenji_time':  tenji_time_score,
                'tenji_st':    tenji_st_score,
            }
        return features

    def _compute_probs(self, features):
        w = self.weights
        scores = {}
        for n, f in features.items():
            scores[n] = (w.w_course      * f['course'] +
                         w.w_player_rate * f['player_rate'] +
                         w.w_local_rate  * f['local_rate'] +
                         w.w_motor_rate  * f['motor_rate'] +
                         w.w_tenji_time  * f['tenji_time'] +
                         w.w_tenji_st    * f['tenji_st'])
        exp = {n: math.exp(s * 3) for n, s in scores.items()}
        total = sum(exp.values()) or 1
        return {n: v / total for n, v in exp.items()}

    def _kelly_bet(self, probs, odds, bankroll=300):
        """
        Kelly基準で最適ベット艇と賭け金を決定する。
        odds: {boat_num: payout_per_100} (例: {1: 250, 3: 820})
        Returns: (bet_boat, bet_amount, ev_per_100, kelly_fraction)
        """
        best = (None, 0, 0.0, 0.0)
        for boat, p in probs.items():
            if boat not in odds or p <= 0:
                continue
            payout = odds[boat]   # 100円賭けたときの払戻額
            if payout <= 100:
                continue
            b = (payout - 100) / 100   # 純利益倍率
            kelly = (b * p - (1 - p)) / b
            ev    = p * payout - 100   # 100円当たりの期待値
            if kelly > best[3]:
                raw_bet  = kelly * bankroll
                bet      = max(100, min(bankroll, round(raw_bet / 100) * 100))
                best     = (boat, bet, ev, kelly)
        return best

    def predict(self, boats, hd, stadium_code, stadium_name, race_no,
                before_info=None, odds=None):
        features = self._extract_features(boats, before_info)
        probs    = self._compute_probs(features)

        # --- Kelly基準でベット判断 ---
        bet_boat, bet_amount, ev_val, kelly = self._kelly_bet(probs, odds or {})

        if bet_boat is not None:
            # EV > 0 → Kelly艇に賭ける（予測艇 = Kelly艇）
            best = bet_boat
        else:
            # EV ≤ 0 → 最高確率艇を予測として記録するが賭けない
            best       = max(probs, key=probs.get)
            bet_amount = 0
            ev_val     = None

        conf = round(probs[best] * 100, 1)

        w            = self.weights
        bet_odds_val = odds.get(bet_boat) if bet_boat and odds else None
        ev_rounded   = round(ev_val, 2) if ev_val is not None else None

        obj, created = RacePrediction.objects.get_or_create(
            date=hd, stadium_code=stadium_code, race_no=race_no,
            defaults={
                'stadium_name':   stadium_name,
                'predicted_boat': best,
                'confidence':     probs[best],
                'boats_json':     json.dumps(boats, ensure_ascii=False),
                'scores_json':    json.dumps({str(k): round(v, 4) for k, v in probs.items()}),
                'w_course':       w.w_course,
                'w_player_rate':  w.w_player_rate,
                'w_local_rate':   w.w_local_rate,
                'w_motor_rate':   w.w_motor_rate,
                'w_tenji_time':   w.w_tenji_time,
                'w_tenji_st':     w.w_tenji_st,
                'bet_amount':     bet_amount,
                'bet_odds':       bet_odds_val,
                'ev_at_bet':      ev_rounded,
            }
        )

        if not created and obj.bet_odds is None and bet_odds_val is not None:
            # 初回作成時はオッズ未取得だったが今回初めてオッズが得られた場合のみ更新
            obj.predicted_boat = best
            obj.confidence     = probs[best]
            obj.bet_amount     = bet_amount
            obj.bet_odds       = bet_odds_val
            obj.ev_at_bet      = ev_rounded
            obj.save(update_fields=[
                'predicted_boat', 'confidence', 'bet_amount', 'bet_odds', 'ev_at_bet'
            ])

        return best, conf, probs

    def update_with_result(self, hd, stadium_code, race_no, actual_winner, payout=None):
        try:
            pred = RacePrediction.objects.get(
                date=hd, stadium_code=stadium_code, race_no=race_no,
                actual_winner__isnull=True,
            )
        except RacePrediction.DoesNotExist:
            return None

        hit = (pred.predicted_boat == actual_winner)
        pred.actual_winner = actual_winner
        pred.hit  = hit
        if payout:
            pred.payout = payout
        pred.save()

        # 外れたレースで重みを更新（payoutを渡して利益重視学習）
        if not hit:
            boats    = json.loads(pred.boats_json)
            features = self._extract_features(boats)
            probs    = self._compute_probs(features)
            self._learn(features, probs, actual_winner, payout)

        return hit

    def _learn(self, features, probs, actual_winner, payout=None):
        """
        収益重視の重み更新。
        高配当レース（payout大）のハズレほど学習強度を上げる。
        """
        w = self.weights

        # 払戻が大きいほど学習強度UP（0.5〜4.0倍）
        profit_weight = max(0.5, min(4.0, payout / 300.0)) if payout else 1.0

        grad = {}
        for feat in FEATURE_KEYS:
            actual_val   = features.get(actual_winner, {}).get(feat, 0)
            expected_val = sum(probs.get(n, 0) * f.get(feat, 0)
                               for n, f in features.items())
            grad[feat] = (actual_val - expected_val) * profit_weight

        w.w_course      = _clamp(w.w_course      + LEARNING_RATE * grad['course'])
        w.w_player_rate = _clamp(w.w_player_rate + LEARNING_RATE * grad['player_rate'])
        w.w_local_rate  = _clamp(w.w_local_rate  + LEARNING_RATE * grad['local_rate'])
        w.w_motor_rate  = _clamp(w.w_motor_rate  + LEARNING_RATE * grad['motor_rate'])
        w.w_tenji_time  = _clamp(w.w_tenji_time  + LEARNING_RATE * grad['tenji_time'])
        w.w_tenji_st    = _clamp(w.w_tenji_st    + LEARNING_RATE * grad['tenji_st'])
        _normalize_weights(w)
        w.race_count += 1
        w.save()
        self.weights = w

    def get_stats(self, n=30):
        preds = list(RacePrediction.objects.filter(hit__isnull=False).order_by('-created_at')[:n])
        if not preds:
            return {'hit_rate': 0, 'count': 0, 'hits': 0}
        hits = sum(1 for p in preds if p.hit)
        return {
            'hit_rate': round(hits / len(preds) * 100, 1),
            'count':    len(preds),
            'hits':     hits,
        }
