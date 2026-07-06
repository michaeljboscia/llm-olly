/**
 * cross_email_checks.js
 *
 * Layer 1 assertion: validates cross-email constraints.
 *   - E2+ must not introduce new signal data (numbers/statistics) beyond E1
 *   - No repeated/copy-pasted content across emails
 *   - Each email should have a distinct angle
 */

module.exports = (output, context) => {
  const errors = [];

  // --- Parse output ---
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

  if (!parsed.emails || !Array.isArray(parsed.emails) || parsed.emails.length < 2) {
    // Can't do cross-email checks with fewer than 2 emails
    return {
      pass: true,
      score: 1,
      reason: 'Fewer than 2 emails — cross-email checks skipped',
    };
  }

  const emails = parsed.emails;

  // --- Extract numbers from text ---
  function extractNumbers(text) {
    if (!text) return [];
    const matches = text.match(/\d+\.?\d*/g);
    return matches ? [...new Set(matches.map(Number))] : [];
  }

  // --- Check E2+ for new signal data beyond E1 ---
  const e1Text = `${emails[0].subject || ''} ${emails[0].body || ''}`;
  const e1Numbers = extractNumbers(e1Text);

  // Also extract numbers from the input payload so we know what's "allowed"
  const inputPayload = typeof context.vars.fixture === 'string'
    ? context.vars.fixture
    : JSON.stringify(context.vars.fixture || {});
  const inputNumbers = extractNumbers(inputPayload);

  // Merge E1 numbers and input numbers as the allowed set
  const allowedNumbers = new Set([...e1Numbers, ...inputNumbers]);

  for (let i = 1; i < emails.length; i++) {
    const emailNum = i + 1;
    const emailText = `${emails[i].subject || ''} ${emails[i].body || ''}`;
    const emailNumbers = extractNumbers(emailText);

    // Filter out trivially common numbers (email sequence numbers, years, small ordinals)
    const significantNewNumbers = emailNumbers.filter((n) => {
      // Skip the email number itself, common years, and small integers (1-10)
      if (n === emailNum) return false;
      if (n >= 2020 && n <= 2030) return false;
      if (n <= 10) return false;
      return !allowedNumbers.has(n);
    });

    if (significantNewNumbers.length > 0) {
      errors.push(
        `E${emailNum} introduces new numbers not in E1 or input: ${significantNewNumbers.join(', ')}`
      );
    }
  }

  // --- Check for repeated content across emails ---
  // Extract sentences from each email body and check for exact duplicates
  function extractSentences(text) {
    if (!text) return [];
    return text
      .split(/[.!?]+/)
      .map((s) => s.trim().toLowerCase())
      .filter((s) => s.length > 20); // Only check substantive sentences
  }

  const allSentences = emails.map((email) => extractSentences(email.body));

  for (let i = 0; i < allSentences.length; i++) {
    for (let j = i + 1; j < allSentences.length; j++) {
      const duplicates = allSentences[i].filter((s) =>
        allSentences[j].includes(s)
      );
      if (duplicates.length > 0) {
        errors.push(
          `E${i + 1} and E${j + 1} share ${duplicates.length} repeated sentence(s): "${duplicates[0].substring(0, 60)}..."`
        );
      }
    }
  }

  // --- Check for duplicate subject lines ---
  const subjects = emails.map((e) => (e.subject || '').trim().toLowerCase());
  const subjectSet = new Set(subjects);
  if (subjectSet.size < subjects.length) {
    errors.push('Duplicate subject lines detected across emails');
  }

  if (errors.length === 0) {
    return {
      pass: true,
      score: 1,
      reason: `Cross-email checks passed (${emails.length} emails, no data leakage or repetition)`,
    };
  }

  return {
    pass: false,
    score: Math.max(0, 1 - errors.length * 0.2),
    reason: `${errors.length} cross-email issue(s):\n- ${errors.join('\n- ')}`,
  };
};
