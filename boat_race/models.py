from django.db import models


class AgentWeight(models.Model):
    """エージェントの学習重み（最新1行を使用）"""
    w_course      = models.FloatField(default=0.30)  # 艇番有利
    w_player_rate = models.FloatField(default=0.18)  # 選手全国勝率
    w_local_rate  = models.FloatField(default=0.12)  # 選手当地勝率
    w_motor_rate  = models.FloatField(default=0.12)  # モーター2連対率
    w_tenji_time  = models.FloatField(default=0.13)  # 展示タイム（速さ）
    w_tenji_st    = models.FloatField(default=0.15)  # 展示スタートタイミング
    race_count    = models.IntegerField(default=0)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return (f"course:{self.w_course:.2f} player:{self.w_player_rate:.2f} "
                f"local:{self.w_local_rate:.2f} motor:{self.w_motor_rate:.2f} "
                f"tenji_t:{self.w_tenji_time:.2f} tenji_st:{self.w_tenji_st:.2f} "
                f"n={self.race_count}")


class RacePrediction(models.Model):
    """レースごとのエージェント予想と結果"""
    date         = models.CharField(max_length=8)
    stadium_code = models.CharField(max_length=2)
    stadium_name = models.CharField(max_length=20)
    race_no      = models.IntegerField()

    predicted_boat = models.IntegerField()
    confidence     = models.FloatField()
    boats_json     = models.TextField(default='[]')
    scores_json    = models.TextField(default='{}')

    actual_winner = models.IntegerField(null=True, blank=True)
    hit           = models.BooleanField(null=True, blank=True)
    payout        = models.IntegerField(null=True, blank=True)

    # 予想時点の重み（記録用）
    w_course      = models.FloatField()
    w_player_rate = models.FloatField()
    w_local_rate  = models.FloatField(default=0.12)
    w_motor_rate  = models.FloatField()
    w_tenji_time  = models.FloatField(default=0.13)
    w_tenji_st    = models.FloatField(default=0.15)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('date', 'stadium_code', 'race_no')
        ordering = ['-created_at']
