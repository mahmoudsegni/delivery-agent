import { describe, it, expect } from 'vitest';
import { extractJSON } from './helpers';

describe('extractJSON', () => {
  it('parses a plain JSON object', () => {
    expect(extractJSON('{"a": 1}')).toEqual({ a: 1 });
  });

  it('parses the object out of junk before and after', () => {
    expect(extractJSON('Sure! Here you go: {"a": {"b": 2}} Hope that helps')).toEqual({ a: { b: 2 } });
  });

  it('parses a fenced ```json block', () => {
    expect(extractJSON('```json\n{"decision": "yes"}\n```')).toEqual({ decision: 'yes' });
  });

  it('does not count braces inside strings or escaped quotes', () => {
    expect(extractJSON('{"code": "if (x) { return \\"}\\"; }"} trailing')).toEqual({
      code: 'if (x) { return "}"; }',
    });
  });

  it('throws on empty input', () => {
    expect(() => extractJSON('')).toThrow('Empty AI response');
  });

  it('throws when there is no object', () => {
    expect(() => extractJSON('no json here')).toThrow('No JSON object');
  });

  it('throws on an unterminated object', () => {
    expect(() => extractJSON('{"a": 1')).toThrow('Incomplete JSON object');
  });
});
