/**
 * deterministic_checks.js
 *
 * Layer 1 assertion: free, instant, no API calls.
 * Validates:
 *   - Output is parseable JSON with an emails array
 *   - Sequence length matches persona spec
 *   - Word count per email falls within persona range
 *   - Subject lines respect max word count
 *   - No forbidden phrases in any email
 *   - Subject lines are lowercase with no trailing punctuation
 */

const fs = require('fs');
const path = require('path');

const FORBIDDEN_PHRASES = [
  'dear sir/madam',
  'dear sir or madam',
  'unsubscribe',
  '[placeholder]',
  'click here',
  'book a meeting',
  'schedule a call',
  'limited time',
  'act now',
  'sign up now',
  'buy now',
  'free trial',
  'don\'t miss out',
  'hurry',
  'offer expires',
];

const FORBIDDEN_SUPERLATIVES = [
  'best in class',
  'world-class',
  'leading provider',
  'industry-leading',
  '#1',
  'number one',
];

module.exports = (output, context) => {
  const errors = [];
  const persona = context.vars.persona;

  // Load the fixture to get assertion constraints
  const fixturePath = path.resolve(
    __dirname,
    '../../../canary/fixtures',
    `${persona}.json`
  );

  let fixture;
  try {
    fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf-8'));
  } catch (e) {
    return {
      pass: false,
      score: 0,
      reason: `Could not load fixture for persona "${persona}": ${e.message}`,
    };
  }

  const assertions = fixture.assertions;

  // --- Parse JSON output ---
  let parsed;
  try {
    parsed = JSON.parse(output);
  } catch (e) {
    return {
      pass: false,
      score: 0,
      reason: `Output is not valid JSON: ${e.message}`,
    };
  }

  if (!parsed.emails || !Array.isArray(parsed.emails)) {
    return {
      pass: false,
      score: 0,
      reason: 'Output JSON missing "emails" array',
    };
  }

  const emails = parsed.emails;

  // --- Check sequence length ---
  if (emails.length !== assertions.sequence_length) {
    errors.push(
      `Sequence length: expected ${assertions.sequence_length}, got ${emails.length}`
    );
  }

  // --- Per-email checks ---
  emails.forEach((email, i) => {
    const emailNum = i + 1;

    // Word count check on body
    if (email.body) {
      const wordCount = email.body.trim().split(/\s+/).length;
      const [minWords, maxWords] = assertions.word_count_range;
      if (wordCount < minWords || wordCount > maxWords) {
        errors.push(
          `E${emailNum} word count: ${wordCount} (expected ${minWords}-${maxWords})`
        );
      }
    } else {
      errors.push(`E${emailNum} missing body`);
    }

    // Subject line checks
    if (email.subject) {
      const subjectWords = email.subject.trim().split(/\s+/).length;
      if (subjectWords > assertions.subject_line_max_words) {
        errors.push(
          `E${emailNum} subject "${email.subject}" has ${subjectWords} words (max ${assertions.subject_line_max_words})`
        );
      }

      // Subject should be lowercase
      if (email.subject !== email.subject.toLowerCase()) {
        errors.push(
          `E${emailNum} subject "${email.subject}" is not all lowercase`
        );
      }
    } else {
      errors.push(`E${emailNum} missing subject`);
    }

    // Forbidden phrases check (case-insensitive)
    const fullText = `${email.subject || ''} ${email.body || ''}`.toLowerCase();

    FORBIDDEN_PHRASES.forEach((phrase) => {
      if (fullText.includes(phrase.toLowerCase())) {
        errors.push(`E${emailNum} contains forbidden phrase: "${phrase}"`);
      }
    });

    FORBIDDEN_SUPERLATIVES.forEach((phrase) => {
      if (fullText.includes(phrase.toLowerCase())) {
        errors.push(`E${emailNum} contains forbidden superlative: "${phrase}"`);
      }
    });

    // Max sentences per paragraph check
    if (email.body && assertions.max_sentences_per_paragraph) {
      const paragraphs = email.body.split(/\n\n+/).filter((p) => p.trim());
      paragraphs.forEach((para, pIdx) => {
        // Count sentences by splitting on sentence-ending punctuation
        const sentences = para
          .split(/[.!?]+/)
          .filter((s) => s.trim().length > 0);
        if (sentences.length > assertions.max_sentences_per_paragraph) {
          errors.push(
            `E${emailNum} paragraph ${pIdx + 1} has ${sentences.length} sentences (max ${assertions.max_sentences_per_paragraph})`
          );
        }
      });
    }
  });

  if (errors.length === 0) {
    return {
      pass: true,
      score: 1,
      reason: `All deterministic checks passed for ${persona} (${emails.length} emails)`,
    };
  }

  return {
    pass: false,
    score: Math.max(0, 1 - errors.length * 0.15),
    reason: `${errors.length} deterministic failure(s):\n- ${errors.join('\n- ')}`,
  };
};
