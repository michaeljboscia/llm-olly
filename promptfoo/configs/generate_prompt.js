/**
 * generate_prompt.js
 *
 * Promptfoo prompt function. Loads the canary fixture for the given persona,
 * extracts prospect data and assertion constraints, and assembles a full
 * system + user prompt for the Narrative Generator.
 *
 * Called by Promptfoo with the test vars object. Returns a prompt array
 * (system + user messages) for the Anthropic messages API.
 */

const fs = require('fs');
const path = require('path');

// Cache loaded fixtures to avoid re-reading disk per call
const fixtureCache = {};

function loadFixture(personaId) {
  if (fixtureCache[personaId]) return fixtureCache[personaId];

  const fixturePath = path.resolve(
    __dirname,
    '../../canary/fixtures',
    `${personaId}.json`
  );

  if (!fs.existsSync(fixturePath)) {
    throw new Error(`Fixture not found: ${fixturePath}`);
  }

  const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf-8'));
  fixtureCache[personaId] = fixture;
  return fixture;
}

/**
 * Build the system prompt with persona-specific constraints.
 */
function buildSystemPrompt(fixture) {
  const { seniority, department, assertions } = fixture;
  const { tone, word_count_range, subject_line_max_words, sequence_length, framework, max_sentences_per_paragraph } = assertions;

  return `You are a cold email copywriter for a B2B ecommerce services company. You generate personalized cold email sequences based on prospect data and pain signals.

PERSONA CONSTRAINTS:
- Seniority level: ${seniority}
- Department: ${department}
- Tone: ${tone}
- Framework: ${framework}
- Word count per email: ${word_count_range[0]}-${word_count_range[1]} words
- Subject line: maximum ${subject_line_max_words} words, all lowercase, no punctuation
- Sequence length: ${sequence_length} emails
- Max sentences per paragraph: ${max_sentences_per_paragraph}

OUTPUT FORMAT:
Return valid JSON with this structure:
{
  "emails": [
    {
      "email_number": 1,
      "subject": "subject line here",
      "body": "email body here"
    }
  ]
}

RULES:
- Every number or statistic in the email MUST come from the prospect data provided. Do NOT invent numbers.
- No placeholder brackets like [Company] or [Name].
- No generic salutations like "Dear Sir/Madam".
- No spam phrases: "click here", "book a meeting", "schedule a call", "limited time", "act now", "sign up now", "buy now", "free trial".
- No superlatives: "best", "leading", "world-class", "#1".
- No presumptuous familiarity: "I know you're busy", "I'm sure you agree".
- One CTA per email maximum.
- Each email in the sequence should have a distinct angle — do not repeat content across emails.
- Email 2+ should NOT introduce new signal data or statistics beyond what Email 1 presents.`;
}

/**
 * Build the user prompt with prospect-specific data.
 */
function buildUserPrompt(fixture) {
  const { prospect_input } = fixture;
  const { domain, company_name, industry, employee_count, angles, contact } = prospect_input;

  const anglesFormatted = angles.map((a, i) => {
    return `Signal ${i + 1} (${a.angle_type}):\n${JSON.stringify(a.angle_data, null, 2)}`;
  }).join('\n\n');

  return `Generate a cold email sequence for this prospect:

COMPANY:
- Name: ${company_name}
- Domain: ${domain}
- Industry: ${industry}
- Employee count: ${employee_count}

CONTACT:
- First name: ${contact.first_name}
- Title: ${contact.title}

PAIN SIGNALS:
${anglesFormatted}

Generate the full email sequence now.`;
}

/**
 * Main prompt function — Promptfoo calls this with { vars }.
 * Returns an array of messages for the Anthropic messages API.
 */
module.exports = function generatePrompt({ vars }) {
  const personaId = vars.persona;

  if (!personaId) {
    throw new Error('Missing required var: persona');
  }

  const fixture = loadFixture(personaId);
  const systemPrompt = buildSystemPrompt(fixture);
  const userPrompt = buildUserPrompt(fixture);

  return [
    { role: 'system', content: systemPrompt },
    { role: 'user', content: userPrompt },
  ];
};
