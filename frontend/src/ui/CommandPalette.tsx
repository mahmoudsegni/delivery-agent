import { useEffect, useMemo, useRef, useState } from 'react';
import { Modal } from './Modal';
import { useSearch } from '../hooks/useApi';
import type { MemberHit, TaskHit } from '../types';

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  onSelectTask: (t: TaskHit) => void;
  onSelectMember: (m: MemberHit) => void;
}

/** Ctrl/Cmd+K palette: substring search over tasks + teammates, keyboard-first. */
export function CommandPalette({ open, onClose, onSelectTask, onSelectMember }: CommandPaletteProps) {
  const [q, setQ] = useState('');
  const [index, setIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const { data } = useSearch(q, open);

  const flat = useMemo(
    () => [
      ...(data?.tasks ?? []).map((t) => ({ kind: 'task' as const, hit: t })),
      ...(data?.members ?? []).map((m) => ({ kind: 'member' as const, hit: m })),
    ],
    [data],
  );

  useEffect(() => setIndex(0), [q]);
  useEffect(() => {
    if (open) {
      setQ('');
      setIndex(0);
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);
  if (!open) return null;

  const pick = (i: number) => {
    const item = flat[i];
    if (!item) return;
    onClose();
    if (item.kind === 'task') onSelectTask(item.hit);
    else onSelectMember(item.hit);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setIndex((i) => Math.min(i + 1, flat.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      pick(index);
    } else if (e.key === 'Escape') {
      onClose();
    }
  };

  const tasks = data?.tasks ?? [];
  const members = data?.members ?? [];

  return (
    <Modal title="Search" onClose={onClose} width={540}>
      <input
        ref={inputRef}
        className="form-input palette-input"
        placeholder="Search tasks and teammates…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={onKeyDown}
      />
      {q.trim().length < 2 ? (
        <p className="palette-hint">Type at least 2 characters. ↑↓ to move, Enter to open, Esc to close.</p>
      ) : flat.length === 0 ? (
        <p className="palette-hint">No matches.</p>
      ) : (
        <div className="palette-list">
          {tasks.length > 0 && (
            <div className="palette-section" key="tasks">
              <div className="palette-section-label">Tasks</div>
              {tasks.map((t, i) => (
                <button
                  key={t.id}
                  className={`palette-item${i === index ? ' active' : ''}`}
                  onMouseEnter={() => setIndex(i)}
                  onClick={() => pick(i)}
                >
                  <span className="palette-item-title">{t.title}</span>
                  <span className="palette-item-meta">
                    {[t.status, t.sprint_name, t.assignee_name].filter(Boolean).join(' · ')}
                  </span>
                </button>
              ))}
            </div>
          )}
          {members.length > 0 && (
            <div className="palette-section" key="members">
              <div className="palette-section-label">Team</div>
              {members.map((m, j) => {
                const i = tasks.length + j;
                return (
                  <button
                    key={m.id}
                    className={`palette-item${i === index ? ' active' : ''}`}
                    onMouseEnter={() => setIndex(i)}
                    onClick={() => pick(i)}
                  >
                    <span className="palette-item-title">{m.name}</span>
                    <span className="palette-item-meta">{m.role}</span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}
