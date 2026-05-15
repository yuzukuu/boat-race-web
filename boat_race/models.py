from django.db import models


class AgentWeight(models.Model):
    """エージェントの学習重み（最新1行を常に使用）"""
    w_course = models.FloatField(default=0.40)       # 艇番有利
    w_player_rate = models.FloatField(default=0.35)  # 選手勝率
    w_motor_rate = models.FloatField(default=0.25)   # モーター勝率
    race_count = models.IntegerField(default=0)      # 学習済みレース数
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return (f"course:{self.w_course:.2f} player:{self.w_player_rate:.2f} "
                f"motor:{self.w_motor_rate:.2f} n={self.race_count}")


class RacePrediction(models.Model):
    """レースごとのエージェント予想と結果"""
    date = models.CharField(max_length=8)
    stadium_code = models.CharField(max_length=2)
    stadium_name = models.CharField(max_length=20)
    race_no = models.IntegerField()

    predicted_boat = models.IntegerField()
    confidence = models.FloatField()
    boats_json = models.TextField(default='[]')   # 全艇の特徴量
    scores_json = models.TextField(default='{}')  # 全艇のスコア確率

    actual_winner = models.IntegerField(null=True, blank=True)
    hit = models.BooleanField(null=True, blank=True)
    payout = models.IntegerField(null=True, blank=True)

    # 予想時点での重み（学習推移の記録用）
    w_course = models.FloatField()
    w_player_rate = models.FloatField()
    w_motor_rate = models.FloatField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('date', 'stadium_code', 'race_no')
        ordering = ['-created_at']
