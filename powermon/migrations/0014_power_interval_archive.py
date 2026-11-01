# Hand-written for quick task 261101-mm9 (DATA-01, DATA-02, DATA-03 amended): the
# append-only power_interval_archive table. A history reset and an outage removal copy the
# rows they take off the timeline into it first, so no real outage history is ever lost.
#
# Additive and reversible: CreateModel only (DeleteModel on the way back);
# tests/chart/test_bot_key_migration.py migrates back and forth across it. Code at de75201
# does not know the table and simply leaves it unused, so a rollback stays safe. The
# deferred F-09 monitoring_since migration moves to 0015.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('powermon', '0013_location_boot_delay_s'),
    ]

    operations = [
        migrations.CreateModel(
            name='PowerIntervalArchive',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('state', models.CharField(max_length=16)),
                ('start_at', models.DateTimeField()),
                ('end_at', models.DateTimeField(null=True)),
                ('outage_start_at', models.DateTimeField(null=True)),
                ('archived_at', models.DateTimeField()),
                ('reason', models.CharField(choices=[('history_reset', 'history reset'), ('outage_removed', 'outage removed')], max_length=16)),
                ('location', models.ForeignKey(db_column='location_id', on_delete=django.db.models.deletion.CASCADE, related_name='archived_intervals', to='powermon.location')),
            ],
            options={
                'db_table': 'power_interval_archive',
                'indexes': [models.Index(fields=['location', 'start_at'], name='interval_archive_loc_start')],
                'constraints': [models.CheckConstraint(condition=models.Q(('state__in', ('on', 'off', 'not_monitored'))), name='power_interval_archive_state_valid'), models.CheckConstraint(condition=models.Q(('reason__in', ('history_reset', 'outage_removed'))), name='power_interval_archive_reason_valid')],
            },
        ),
    ]
