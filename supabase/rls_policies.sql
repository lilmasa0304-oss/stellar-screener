-- Supabase Row Level Security (RLS) for STELLAR SCREENER
--
-- 実行方法: Supabase Dashboard → SQL Editor でこのファイルを実行
--
-- 注意:
-- - FastAPI は DATABASE_URL の postgres ロールで接続するため RLS をバイパスします。
--   アプリ側でも user_id フィルタを必ず行ってください（二重防御）。
-- - 本ポリシーは Supabase Client / PostgREST 経由の直接アクセスを保護します。

-- user_id カラム（未追加の場合）
ALTER TABLE IF EXISTS scan_sessions ADD COLUMN IF NOT EXISTS user_id TEXT;
ALTER TABLE IF EXISTS scan_results ADD COLUMN IF NOT EXISTS user_id TEXT;
ALTER TABLE IF EXISTS signal_tracks ADD COLUMN IF NOT EXISTS user_id TEXT;

CREATE INDEX IF NOT EXISTS idx_ss_user_id ON scan_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sr_user_id ON scan_results(user_id);
CREATE INDEX IF NOT EXISTS idx_st_user_id ON signal_tracks(user_id);

-- RLS 有効化
ALTER TABLE scan_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE scan_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE signal_tracks ENABLE ROW LEVEL SECURITY;
ALTER TABLE signal_track_outcomes ENABLE ROW LEVEL SECURITY;

-- 既存ポリシーを削除（再実行用）
DROP POLICY IF EXISTS "scan_sessions_select_own" ON scan_sessions;
DROP POLICY IF EXISTS "scan_sessions_insert_own" ON scan_sessions;
DROP POLICY IF EXISTS "scan_sessions_update_own" ON scan_sessions;
DROP POLICY IF EXISTS "scan_sessions_delete_own" ON scan_sessions;

DROP POLICY IF EXISTS "scan_results_select_own" ON scan_results;
DROP POLICY IF EXISTS "scan_results_insert_own" ON scan_results;
DROP POLICY IF EXISTS "scan_results_update_own" ON scan_results;
DROP POLICY IF EXISTS "scan_results_delete_own" ON scan_results;

DROP POLICY IF EXISTS "signal_tracks_select_own" ON signal_tracks;
DROP POLICY IF EXISTS "signal_tracks_insert_own" ON signal_tracks;
DROP POLICY IF EXISTS "signal_tracks_update_own" ON signal_tracks;
DROP POLICY IF EXISTS "signal_tracks_delete_own" ON signal_tracks;

DROP POLICY IF EXISTS "signal_track_outcomes_select_own" ON signal_track_outcomes;
DROP POLICY IF EXISTS "signal_track_outcomes_insert_own" ON signal_track_outcomes;
DROP POLICY IF EXISTS "signal_track_outcomes_update_own" ON signal_track_outcomes;
DROP POLICY IF EXISTS "signal_track_outcomes_delete_own" ON signal_track_outcomes;

-- scan_sessions
CREATE POLICY "scan_sessions_select_own" ON scan_sessions
  FOR SELECT USING (user_id = auth.uid()::text);

CREATE POLICY "scan_sessions_insert_own" ON scan_sessions
  FOR INSERT WITH CHECK (user_id = auth.uid()::text);

CREATE POLICY "scan_sessions_update_own" ON scan_sessions
  FOR UPDATE USING (user_id = auth.uid()::text)
  WITH CHECK (user_id = auth.uid()::text);

CREATE POLICY "scan_sessions_delete_own" ON scan_sessions
  FOR DELETE USING (user_id = auth.uid()::text);

-- scan_results
CREATE POLICY "scan_results_select_own" ON scan_results
  FOR SELECT USING (user_id = auth.uid()::text);

CREATE POLICY "scan_results_insert_own" ON scan_results
  FOR INSERT WITH CHECK (user_id = auth.uid()::text);

CREATE POLICY "scan_results_update_own" ON scan_results
  FOR UPDATE USING (user_id = auth.uid()::text)
  WITH CHECK (user_id = auth.uid()::text);

CREATE POLICY "scan_results_delete_own" ON scan_results
  FOR DELETE USING (user_id = auth.uid()::text);

-- signal_tracks
CREATE POLICY "signal_tracks_select_own" ON signal_tracks
  FOR SELECT USING (user_id = auth.uid()::text);

CREATE POLICY "signal_tracks_insert_own" ON signal_tracks
  FOR INSERT WITH CHECK (user_id = auth.uid()::text);

CREATE POLICY "signal_tracks_update_own" ON signal_tracks
  FOR UPDATE USING (user_id = auth.uid()::text)
  WITH CHECK (user_id = auth.uid()::text);

CREATE POLICY "signal_tracks_delete_own" ON signal_tracks
  FOR DELETE USING (user_id = auth.uid()::text);

-- signal_track_outcomes（親 track の user_id で判定）
CREATE POLICY "signal_track_outcomes_select_own" ON signal_track_outcomes
  FOR SELECT USING (
    EXISTS (
      SELECT 1 FROM signal_tracks t
      WHERE t.track_id = signal_track_outcomes.track_id
        AND t.user_id = auth.uid()::text
    )
  );

CREATE POLICY "signal_track_outcomes_insert_own" ON signal_track_outcomes
  FOR INSERT WITH CHECK (
    EXISTS (
      SELECT 1 FROM signal_tracks t
      WHERE t.track_id = signal_track_outcomes.track_id
        AND t.user_id = auth.uid()::text
    )
  );

CREATE POLICY "signal_track_outcomes_update_own" ON signal_track_outcomes
  FOR UPDATE USING (
    EXISTS (
      SELECT 1 FROM signal_tracks t
      WHERE t.track_id = signal_track_outcomes.track_id
        AND t.user_id = auth.uid()::text
    )
  )
  WITH CHECK (
    EXISTS (
      SELECT 1 FROM signal_tracks t
      WHERE t.track_id = signal_track_outcomes.track_id
        AND t.user_id = auth.uid()::text
    )
  );

CREATE POLICY "signal_track_outcomes_delete_own" ON signal_track_outcomes
  FOR DELETE USING (
    EXISTS (
      SELECT 1 FROM signal_tracks t
      WHERE t.track_id = signal_track_outcomes.track_id
        AND t.user_id = auth.uid()::text
    )
  );
