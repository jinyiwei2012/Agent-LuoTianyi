import { useCallback, useEffect, useState } from 'react';
import {
  createDynamic,
  createDynamicComment,
  getDynamicComments,
  getDynamicSourceLabel,
  getDynamics,
  getDynamicUnreadStatus,
  markDynamicsRead,
} from '@/utils/dynamics';
import type { DynamicComment, DynamicPost } from '@/utils/dynamics';

type CommentBucket = {
  readonly items: DynamicComment[];
  readonly loading: boolean;
  readonly loaded: boolean;
  readonly error: string;
};

export interface DynamicsPageProps {
  username: string;
  messageToken: string;
  onBack: () => void;
  onUnreadCleared: () => void;
}

function formatDate(value: string | null): string {
  if (!value) {
    return '刚刚';
  }
  return new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value));
}

function authorLabel(authorType: string, authorName: string): string {
  if (authorType === 'agent') {
    return '洛天依';
  }
  if (authorType === 'system') {
    return '系统';
  }
  return authorName || '你';
}

function DynamicPostCard({
  post,
  bucket,
  commentDraft,
  onToggleComments,
  onCommentDraftChange,
  onSubmitComment,
}: {
  post: DynamicPost;
  bucket?: CommentBucket;
  commentDraft: string;
  onToggleComments: () => void;
  onCommentDraftChange: (value: string) => void;
  onSubmitComment: () => void;
}) {
  const commentsOpen = bucket !== undefined;
  return (
    <article className="dynamic-card">
      <header className="dynamic-card__header">
        <div className={`dynamic-avatar dynamic-avatar--${post.author_type}`} aria-hidden="true">{post.author_type === 'agent' ? '洛' : post.author_type === 'system' ? '·' : '你'}</div>
        <div><h2>{authorLabel(post.author_type, post.author_name)}</h2><p>{getDynamicSourceLabel(post.source_type)} · <time>{formatDate(post.created_at)}</time></p></div>
        <span className="dynamic-visibility">{post.visibility === 'global' ? '公开' : '仅你可见'}</span>
      </header>
      <p className="dynamic-card__content">{post.content}</p>
      {post.author_type === 'user' && post.reply_status === 'pending' && <p className="dynamic-card__pending">天依稍后会来看这条动态</p>}
      <footer className="dynamic-card__footer"><button className="text-button" type="button" onClick={onToggleComments}>{commentsOpen ? '收起评论' : `查看评论 ${post.comment_count > 0 ? `(${post.comment_count})` : ''}`}</button></footer>
      {commentsOpen && <section className="comment-thread" aria-label="评论"><div className="comment-list">{bucket.loading ? <p className="inline-status">评论加载中…</p> : bucket.error ? <p className="form-notice form-notice--error">{bucket.error}</p> : bucket.items.length === 0 ? <p className="inline-status">还没有评论，来坐第一排吧。</p> : bucket.items.map((comment) => <div className="comment-item" key={comment.id}><strong>{authorLabel(comment.author_type, comment.author_name)}</strong><p>{comment.content}</p><time>{formatDate(comment.created_at)}</time></div>)}</div>{post.allow_comment && <div className="comment-composer"><label className="sr-only" htmlFor={`comment-${post.id}`}>发表评论</label><input id={`comment-${post.id}`} value={commentDraft} onChange={(event) => onCommentDraftChange(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && commentDraft.trim()) onSubmitComment(); }} placeholder="写下评论…" /><button className="secondary-button secondary-button--compact" type="button" disabled={!commentDraft.trim()} onClick={onSubmitComment}>发送</button></div>}</section>}
    </article>
  );
}

export function DynamicsPage({ username, messageToken, onBack, onUnreadCleared }: DynamicsPageProps) {
  const [posts, setPosts] = useState<DynamicPost[]>([]);
  const [comments, setComments] = useState<Record<string, CommentBucket>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [postDraft, setPostDraft] = useState('');
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [unreadSeen, setUnreadSeen] = useState(0);
  const [error, setError] = useState('');

  const loadPosts = useCallback(async (cursor: string | null = null) => {
    const result = await getDynamics(username, messageToken, 20, cursor);
    setPosts((current) => cursor ? [...current, ...result.items] : result.items);
    setNextCursor(result.next_cursor);
    setHasMore(result.has_more);
    setError('');
  }, [messageToken, username]);

  useEffect(() => {
    let active = true;
    const initialize = async () => {
      try {
        const unread = await getDynamicUnreadStatus(username, messageToken);
        if (active) setUnreadSeen(unread.unread_count);
      } catch (caught) {
        if (active) setError(caught instanceof Error ? caught.message : '未读状态读取失败');
      }
      try {
        await loadPosts();
        await markDynamicsRead(username, messageToken);
        if (active) onUnreadCleared();
      } catch (caught) {
        if (active) setError(caught instanceof Error ? caught.message : '动态加载失败');
      } finally {
        if (active) setLoading(false);
      }
    };
    void initialize();
    return () => { active = false; };
  }, [loadPosts, messageToken, onUnreadCleared, username]);

  const publishPost = async () => {
    const content = postDraft.trim();
    if (!content || submitting) return;
    setSubmitting(true);
    try {
      const created = await createDynamic(username, messageToken, content);
      setPosts((current) => [created, ...current]);
      setPostDraft('');
      setError('');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '发布失败');
    } finally {
      setSubmitting(false);
    }
  };

  const toggleComments = async (postId: string) => {
    if (comments[postId]) {
      setComments((current) => { const next = { ...current }; delete next[postId]; return next; });
      return;
    }
    setComments((current) => ({ ...current, [postId]: { items: [], loading: true, loaded: false, error: '' } }));
    try {
      const result = await getDynamicComments(username, messageToken, postId, 50);
      setComments((current) => ({ ...current, [postId]: { items: result.items, loading: false, loaded: true, error: '' } }));
    } catch (caught) {
      setComments((current) => ({ ...current, [postId]: { items: [], loading: false, loaded: true, error: caught instanceof Error ? caught.message : '评论加载失败' } }));
    }
  };

  const submitComment = async (postId: string) => {
    const content = (drafts[postId] ?? '').trim();
    if (!content) return;
    try {
      const created = await createDynamicComment(username, messageToken, postId, content);
      setComments((current) => ({ ...current, [postId]: { items: [...(current[postId]?.items ?? []), created], loading: false, loaded: true, error: '' } }));
      setDrafts((current) => ({ ...current, [postId]: '' }));
      setPosts((current) => current.map((post) => post.id === postId ? { ...post, comment_count: post.comment_count + 1 } : post));
    } catch (caught) {
      setComments((current) => ({ ...current, [postId]: { items: current[postId]?.items ?? [], loading: false, loaded: true, error: caught instanceof Error ? caught.message : '评论发送失败' } }));
    }
  };

  const loadMore = async () => {
    if (!hasMore || !nextCursor || loadingMore) return;
    setLoadingMore(true);
    try { await loadPosts(nextCursor); } catch (caught) { setError(caught instanceof Error ? caught.message : '加载更多失败'); } finally { setLoadingMore(false); }
  };

  return (
    <main className="subpage-shell"><header className="subpage-header"><button className="secondary-button" type="button" onClick={onBack}>返回聊天</button><div><p className="eyebrow">TIANYI MOMENTS</p><h1>天依动态</h1><p>聊天之外，也看看彼此今天遇见了什么。</p></div><button className="secondary-button" type="button" onClick={() => void loadPosts()}>刷新</button></header><div className="subpage-scroll"><div className="subpage-content subpage-content--feed"><section className="dynamic-composer"><div className="dynamic-composer__author"><div className="dynamic-avatar dynamic-avatar--user" aria-hidden="true">你</div><div><strong>分享此刻</strong>{unreadSeen > 0 && <span>刚刚看过 {unreadSeen} 条新动态</span>}</div></div><label className="field"><span className="sr-only">动态内容</span><textarea value={postDraft} onChange={(event) => setPostDraft(event.target.value)} placeholder="今天发生了什么？写给天依看看…" rows={3} maxLength={500} /></label><div className="dynamic-composer__footer"><span>{postDraft.length}/500</span><button className="primary-button" type="button" disabled={!postDraft.trim() || submitting} onClick={() => void publishPost()}>{submitting ? '发布中…' : '发布动态'}</button></div></section>{error && <p className="form-notice form-notice--error" role="alert">{error}</p>}{loading ? <div className="page-loader"><span /><p>正在翻开动态簿…</p></div> : posts.length === 0 ? <div className="empty-card"><h2>还没有动态</h2><p>第一条可以从今天的心情开始。</p></div> : <section className="dynamic-feed" aria-label="动态列表">{posts.map((post) => <DynamicPostCard key={post.id} post={post} bucket={comments[post.id]} commentDraft={drafts[post.id] ?? ''} onToggleComments={() => void toggleComments(post.id)} onCommentDraftChange={(value) => setDrafts((current) => ({ ...current, [post.id]: value }))} onSubmitComment={() => void submitComment(post.id)} />)}{hasMore && <button className="secondary-button load-more-button" type="button" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? '加载中…' : '加载更多'}</button>}</section>}</div></div></main>
  );
}
