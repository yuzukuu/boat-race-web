import json
import math
from .models import AgentWeight, RacePrediction

# ボートレース統計に基づくコース有利係数
COURSE_ADVANTAGE = {1: 1.00, 2: 0.35, 3: 0.28, 4: 0.25, 5: 0.22, 6: 0.20}
LEARNING_RATE = 0.02
MIN_W, MAX_W = 0.05, 0.85


def _clamp(v):
    return max(MIN_W, min(MAX_W, v))


class PredictionAgent:
    def __init__(self):
        self.weights = self._load_or_create()

    def _load_or_create(self):
        w = AgentWeight.objects.order_by('-updated_at').first()
        if not w:
            w = AgentWeight.objects.create()
        return w

    def _extract_features(self, boats):
        """各艇の特徴量（0〜1正規化）を返す"""
        features = {}
        for boat in boats:
            n = boat['number']
            try:
                pr = min(float(boat.get('win_rate', '3.0')) / 10.0, 1.0)
            except:
                pr = 0.3
            try:
                mr = min(float(boat.get('motor_rate', '30.0')) / 100.0, 1.0)
            except:
                mr = 0.3
            features[n] = {
                'course': COURSE_ADVANTAGE.get(n, 0.20),
                'player_rate': pr,
                'motor_rate': mr,
            }
        return features

    def _compute_probs(self, features):
        """特徴量 × 重みでスコアを計算し softmax 確率に変換"""
        w = self.weights
        scores = {}
        for n, f in features.items():
            scores[n] = (w.w_course * f['course'] +
                         w.w_player_rate * f['player_rate'] +
                         w.w_motor_rate * f['motor_rate'])
        # softmax（温度=3 で差を強調）
        exp = {n: math.exp(s * 3) for n, s in scores.items()}
        total = sum(exp.values())
        return {n: v / total for n, v in exp.items()}

    def predict(self, boats, hd, stadium_code, stadium_name, race_no):
        """予想を実行してDBに保存。(predicted_boat, confidence%, probs) を返す"""
        features = self._extract_features(boats)
        probs = self._compute_probs(features)
        best = max(probs, key=probs.get)
        conf = round(probs[best] * 100, 1)

        w = self.weights
        RacePrediction.objects.update_or_create(
            date=hd, stadium_code=stadium_code, race_no=race_no,
            defaults={
                'stadium_name': stadium_name,
                'predicted_boat': best,
                'confidence': probs[best],
                'boats_json': json.dumps(boats, ensure_ascii=False),
                'scores_json': json.dumps({str(k): round(v, 4) for k, v in probs.items()}),
                'w_course': w.w_course,
                'w_player_rate': w.w_player_rate,
                'w_motor_rate': w.w_motor_rate,
            }
        )
        return best, conf, probs

    def update_with_result(self, hd, stadium_code, race_no, actual_winner, payout=None):
        """結果をDBに記録し、外れた場合に重みを学習する"""
        try:
            pred = RacePrediction.objects.get(
                date=hd, stadium_code=stadium_code, race_no=race_no,
                actual_winner__isnull=True,
            )
        except RacePrediction.DoesNotExist:
            return None

        hit = (pred.predicted_boat == actual_winner)
        pred.actual_winner = actual_winner
        pred.hit = hit
        if payout:
            pred.payout = payout
        pred.save()

        if not hit:
            boats = json.loads(pred.boats_json)
            features = self._extract_features(boats)
            probs = self._compute_probs(features)
            self._learn(features, probs, actual_winner)

        return hit

    def _learn(self, features, probs, actual_winner):
        """交差エントロピー勾配で重みを更新する"""
        w = self.weights
        for feat in ('course', 'player_rate', 'motor_rate'):
            actual_val = features.get(actual_winner, {}).get(feat, 0)
            expected_val = sum(probs.get(n, 0) * f.get(feat, 0)
                               for n, f in features.items())
            grad = actual_val - expected_val
            if feat == 'course':
                w.w_course = _clamp(w.w_course + LEARNING_RATE * grad)
            elif feat == 'player_rate':
                w.w_player_rate = _clamp(w.w_player_rate + LEARNING_RATE * grad)
            elif feat == 'motor_rate':
                w.w_motor_rate = _clamp(w.w_motor_rate + LEARNING_RATE * grad)

        # 合計を1に正規化
        total = w.w_course + w.w_player_rate + w.w_motor_rate
        w.w_course /= total
        w.w_player_rate /= total
        w.w_motor_rate /= total
        w.race_count += 1
        w.save()
        self.weights = w

    def get_stats(self, n=30):
        """最近 n 件の的中率・重みを返す"""
        preds = list(
            RacePrediction.objects.filter(hit__isnull=False).order_by('-created_at')[:n]
        )
        if not preds:
            return {'hit_rate': 0, 'count': 0, 'hits': 0}
        hits = sum(1 for p in preds if p.hit)
        return {
            'hit_rate': round(hits / len(preds) * 100, 1),
            'count': len(preds),
            'hits': hits,
        }
