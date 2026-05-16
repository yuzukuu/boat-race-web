from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('boat_race', '0002_agentweight_w_local_rate_agentweight_w_tenji_st_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='raceprediction',
            name='bet_amount',
            field=models.IntegerField(default=300),
        ),
        migrations.AddField(
            model_name='raceprediction',
            name='bet_odds',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='raceprediction',
            name='ev_at_bet',
            field=models.FloatField(blank=True, null=True),
        ),
    ]
