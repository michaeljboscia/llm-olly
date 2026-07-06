/**
 * no_hallucinated_numbers.js
 *
 * Dedicated hallucination check. Every number in the generated output must
 * trace back to either:
 *   1. The input prospect data (angles, employee_count, etc.)
 *   2. A trivially derivable number (email sequence number, common years)
 *
 * Numbers that appear in the output but NOT in the input are flagged as
 * potentially hallucinated.
 */

const fs = require('fs');
const path = require('path');

module.exports = (output, context) => {
  const persona = context.vars.persona;

  // Load the fixture to get the full input data
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

  // --- Extract all numbers from input prospect data ---
  function extractNumbers(text) {
    if (!text) return [];
    const matches = text.match(/\d+\.?\d*/g);
    return matches ? matches.map(Number) : [];
  }

  // Serialize the entire prospect_input to capture all numbers
  const inputText = JSON.stringify(fixture.prospect_input);
  const inputNumbers = new Set(extractNumbers(inputText));

  // Add commonly acceptable derived numbers
  // - Years in reasonable range
  for (let y = 2020; y <= 2030; y++) inputNumbers.add(y);
  // - Small ordinals and sequence numbers (1-10)
  for (let i = 0; i <= 10; i++) inputNumbers.add(i);
  // - Common percentages that are simple math from input
  // (e.g., if input has 78% and 69%, the difference 9 is derivable)
  const inputArr = [...inputNumbers];
  for (let i = 0; i < inputArr.length; i++) {
    for (let j = i + 1; j < inputArr.length; j++) {
      const diff = Math.abs(inputArr[i] - inputArr[j]);
      if (diff > 0 && diff < 1000) {
        inputNumbers.add(diff);
        inputNumbers.add(Math.round(diff * 10) / 10);
      }
      // Also add ratios that might appear (e.g., "2x" from comparing metrics)
      if (inputArr[j] !== 0) {
        const ratio = Math.round((inputArr[i] / inputArr[j]) * 10) / 10;
        if (ratio > 0 && ratio < 100) inputNumbers.add(ratio);
      }
      if (inputArr[i] !== 0) {
        const ratio = Math.round((inputArr[j] / inputArr[i]) * 10) / 10;
        if (ratio > 0 && ratio < 100) inputNumbers.add(ratio);
      }
    }
  }

  // --- Parse output and extract numbers ---
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

  const hallucinated = [];

  parsed.emails.forEach((email, i) => {
    const emailText = `${email.subject || ''} ${email.body || ''}`;
    const outputNumbers = extractNumbers(emailText);

    outputNumbers.forEach((num) => {
      if (!inputNumbers.has(num)) {
        // Check if it's close to an input number (within rounding)
        let isClose = false;
        for (const inputNum of inputNumbers) {
          if (Math.abs(num - inputNum) < 0.5) {
            isClose = true;
            break;
          }
        }

        if (!isClose) {
          hallucinated.push({
            email: i + 1,
            number: num,
          });
        }
      }
    });
  });

  if (hallucinated.length === 0) {
    return {
      pass: true,
      score: 1,
      reason: `No hallucinated numbers detected across ${parsed.emails.length} emails`,
    };
  }

  // Group by email for cleaner reporting
  const byEmail = {};
  hallucinated.forEach(({ email, number }) => {
    if (!byEmail[email]) byEmail[email] = [];
    byEmail[email].push(number);
  });

  const details = Object.entries(byEmail)
    .map(([emailNum, nums]) => `E${emailNum}: ${nums.join(', ')}`)
    .join('; ');

  return {
    pass: false,
    score: Math.max(0, 1 - hallucinated.length * 0.25),
    reason: `${hallucinated.length} potentially hallucinated number(s): ${details}`,
  };
};
