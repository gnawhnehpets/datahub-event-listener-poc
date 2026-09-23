const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
  Table, TableRow, TableCell, WidthType, ShadingType, BorderStyle,
  LevelFormat, PageBreak,
} = require("docx");
const fs = require("fs");

const MONO = "Consolas";
const ACCENT = "1F4E79";
const GREY = "F2F2F2";
const TABLE_W = 9360; // 6.5in usable on US Letter with 1in margins

const p = (text, opts = {}) => new Paragraph({
  spacing: { after: opts.after ?? 120, before: opts.before ?? 0 },
  alignment: opts.align,
  children: [new TextRun({ text, bold: opts.bold, italics: opts.italics,
                           size: opts.size ?? 22, color: opts.color, font: opts.font })],
});

const h = (text, level) => new Paragraph({
  heading: level, spacing: { before: 280, after: 140 },
  children: [new TextRun({ text, bold: true, color: ACCENT })],
});

const code = (lines) => lines.map((line, i) => new Paragraph({
  spacing: { after: i === lines.length - 1 ? 140 : 0, before: i === 0 ? 60 : 0 },
  shading: { type: ShadingType.CLEAR, fill: GREY },
  indent: { left: 240 },
  children: [new TextRun({ text: line || " ", font: MONO, size: 18 })],
}));

const bullets = (items) => items.map((t) => new Paragraph({
  numbering: { reference: "dash", level: 0 },
  spacing: { after: 80 },
  children: [new TextRun({ text: t, size: 22 })],
}));

const steps = (items) => items.map((t) => new Paragraph({
  numbering: { reference: "steps", level: 0 },
  spacing: { after: 100 },
  children: [new TextRun({ text: t, size: 22 })],
}));

function table(headers, rows, widths) {
  const cell = (text, { bold = false, fill = null, font = null } = {}, w) => new TableCell({
    width: { size: w, type: WidthType.DXA },
    shading: fill ? { type: ShadingType.CLEAR, fill } : undefined,
    margins: { top: 80, bottom: 80, left: 120, right: 120 },
    children: [new Paragraph({
      spacing: { after: 0 },
      children: [new TextRun({ text, bold, size: 19, font })],
    })],
  });
  return new Table({
    width: { size: TABLE_W, type: WidthType.DXA },
    columnWidths: widths,
    rows: [
      new TableRow({
        tableHeader: true,
        children: headers.map((t, i) => cell(t, { bold: true, fill: "DCE6F1" }, widths[i])),
      }),
      ...rows.map((r) => new TableRow({
        children: r.map((c, i) => cell(
          typeof c === "object" ? c.t : c,
          { font: (typeof c === "object" && c.mono) ? MONO : null },
          widths[i],
        )),
      })),
    ],
  });
}

const doc = new Document({
  numbering: {
    config: [
      { reference: "dash", levels: [{ level: 0, format: LevelFormat.BULLET, text: "–",
        alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 460, hanging: 240 } } } }] },
      { reference: "steps", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.",
        alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 460, hanging: 280 } } } }] },
    ],
  },
  sections: [{
    properties: { page: { size: { width: 12240, height: 15840 },
                          margin: { top: 1440, bottom: 1440, left: 1440, right: 1440 } } },
    children: [
      new Paragraph({
        spacing: { after: 60 },
        border: { bottom: { style: BorderStyle.SINGLE, size: 12, color: ACCENT, space: 8 } },
        children: [new TextRun({ text: "Domain-Scoped Approvers for Dataset Access Requests",
                                 bold: true, size: 34, color: ACCENT })],
      }),
      p("DataHub Cloud — Action Workflows · trial instance", { italics: true, color: "595959", after: 40 }),
      p("Setup, rationale, and validated demo · 23 September 2026", { italics: true, color: "595959", after: 260 }),

      h("1. The requirement", HeadingLevel.HEADING_1),
      p("Approvals and notifications must reach the approver responsible for the affected domain. Sending a request to every approver in the organisation does not meet the operating model."),
      p("The reported symptom: assigning an approver group such as Data Steward caused tasks to be assigned to all Data Stewards, rather than only those associated with the domain being modified."),
      p("Question asked: can the workflow dynamically identify the relevant domain and route approvals to the appropriate domain-specific approvers?"),
      p("Answer: yes. The mechanism is built in, and it is now configured and verified on the Dataset Access Request workflow. This document records how, and how to demonstrate it.", { bold: true }),

      h("2. Why the original configuration routed organisation-wide", HeadingLevel.HEADING_1),
      p("The shipped approval step combined two settings, each of which widened the audience independently:"),
      ...code([
        '"users": [{ "urn": "urn:li:corpuser:data.steward" }],',
        '"dynamicAssignment": {',
        '    "type": "ENTITY_OWNERS",',
        '    "ownershipTypes": []',
        '}',
      ]),
      ...bullets([
        "A hardcoded user was attached to the step, so that person was assigned on every request regardless of domain.",
        "ENTITY_OWNERS resolves the owners of the dataset itself, not the owners of its domain.",
        "An empty ownershipTypes list matches every ownership type. This is the most likely source of the reported “assigned to all Data Stewards” behaviour.",
      ]),
      p("This was confirmed against live data rather than inferred. All 23 Dataset Access Requests raised before the change were assigned to the same three people, on every dataset:"),
      ...code(["users: ['urn:li:corpuser:data.steward',",
               "        'urn:li:corpuser:stephen.hwang@datahub.com',",
               "        'urn:li:corpuser:<a second dataset owner>']"]),

      h("3. The mechanism", HeadingLevel.HEADING_1),
      p("DataHub provides a dynamic assignment type for exactly this. Introspecting the live GraphQL schema returns three options, with the platform's own descriptions:"),
      table(
        ["ActionWorkflowStepDynamicAssignmentType", "Description"],
        [
          [{ t: "ENTITY_OWNERS", mono: true }, "Assign the entity owners."],
          [{ t: "ENTITY_DOMAIN_OWNERS", mono: true }, "Assign the entity domain owners."],
          [{ t: "ENTITY_DATA_PRODUCT_OWNERS", mono: true }, "Assign the entity data product owners."],
        ],
        [4200, 5160],
      ),
      p("", { after: 120 }),
      p("ENTITY_DOMAIN_OWNERS resolves the domain of the entity the request was raised against, then takes that domain's owners. Pairing it with ownershipTypeUrns narrows those owners to a single ownership type — here, Data Steward."),
      p("Result: a request raised against a dataset is routed to the Data Steward of that dataset's domain, and to nobody else. No per-domain configuration is required; the workflow reads the ownership already recorded on the domain."),

      h("4. The change applied", HeadingLevel.HEADING_1),
      table(
        ["", "Before", "After"],
        [
          ["Static actors", { t: "users: [data.steward]", mono: true }, { t: "userUrns: []", mono: true }],
          ["Assignment type", { t: "ENTITY_OWNERS", mono: true }, { t: "ENTITY_DOMAIN_OWNERS", mono: true }],
          ["Ownership filter", { t: "ownershipTypes: []  (every type)", mono: true }, { t: "__system__data_steward", mono: true }],
        ],
        [1900, 3500, 3960],
      ),
      p("", { after: 120 }),
      p("The hardcoded user was removed deliberately. Leaving it would assign that person to every request regardless of domain, which is the behaviour being eliminated."),
      p("Deployed to the Dataset Access Request workflow (urn:li:actionWorkflow:7110b19c-45b9-4450-b8bb-ba47d9f522ba) and verified live. The definition is version-controlled at workflows/definitions/action-workflow-dataset-access-request_v2.json."),

      new Paragraph({ children: [new PageBreak()] }),

      h("5. Prerequisite: domain ownership must exist", HeadingLevel.HEADING_1),
      p("This is the part that determines whether the feature works in practice. The routing is only as good as the ownership recorded on each domain.", { bold: true }),
      p("For a request to reach anyone, two things must be true: the dataset must be assigned to a domain, and that domain must have an owner carrying the Data Steward ownership type."),
      p("Current coverage on the trial instance:"),
      table(
        ["Measure", "Count", "Consequence"],
        [
          ["Datasets total", "499", ""],
          ["Datasets assigned to a domain", "29", "470 have no domain — no approver can be resolved"],
          ["Of those, in a domain with a Data Steward", "22", "7 resolve to nobody"],
        ],
        [4000, 1200, 4160],
      ),
      p("", { after: 120 }),
      p("Assigning a single organisation-wide Data Stewards group directly to the step is the anti-pattern that produces the original symptom. Domain-scoped approval instead requires each domain to carry its own steward as an owner. Populating that ownership is a data exercise, not a configuration change."),

      h("6. Demo walkthrough", HeadingLevel.HEADING_1),
      p("This sequence uses one dataset and changes one variable, so the causal link is unambiguous: the workflow, the form, and the entity stay fixed; only the domain's ownership changes, and the approver changes with it."),
      p("Setup used: the domain Finance Division, whose Data Steward was Fiona Green (urn:li:corpuser:sample_data_marty@example.com). Note that this sample user's display name does not match its username, which can be momentarily confusing during a demo."),

      h("Step 0 — Capture the baseline", HeadingLevel.HEADING_2),
      ...code(["set -a && . ./.env && set +a",
               ".venv/bin/python workflows/definitions/show_request_assignees.py"]),
      p("Every historical request lists three approvers. Screenshot this; it is the “before”."),

      h("Step 1 — Raise a request on a domain you do not steward", HeadingLevel.HEADING_2),
      ...steps([
        "Open the dataset ORDER_HISTORY (snowflake), which belongs to Finance Division.",
        "Confirm the right-hand panel shows the domain Finance Division.",
        "Choose Request Access, complete the justification, and submit.",
        "Observe that the task is assigned to Fiona Green and does not appear in your Task Center — even though you are a Data Steward on other domains.",
      ]),
      p("This is the half that matters most. Being a steward somewhere does not place you in every queue.", { italics: true }),

      h("Step 2 — Add yourself as Data Steward of that domain", HeadingLevel.HEADING_2),
      ...steps([
        "Open the Finance Division domain.",
        "Add yourself as an owner with the ownership type Data Steward.",
      ]),

      h("Step 3 — Raise the same request again", HeadingLevel.HEADING_2),
      ...steps([
        "Return to ORDER_HISTORY and submit another access request.",
        "The task now lands in your Task Center.",
      ]),
      p("Same dataset, same workflow, same form. Only the domain ownership changed, and the routing followed it."),

      h("Step 4 — Show the objective record", HeadingLevel.HEADING_2),
      ...code([".venv/bin/python workflows/definitions/show_request_assignees.py"]),
      p("The Task Center can only show a reviewer their own queue, which cannot demonstrate that a request did not also go to everyone else. Reading the assignees off each request shows both halves at once."),

      h("7. Evidence from the validated run", HeadingLevel.HEADING_1),
      table(
        ["Date", "Dataset / platform", "Domain", "Assigned to"],
        [
          ["17 Sep", "banking_loans (databricks)", "—", "data.steward + 2 dataset owners"],
          ["17 Sep", "loans_banking (databricks)", "—", "data.steward + 2 dataset owners"],
          ["23 Sep", "customers (dbt)", "Data Platform Team", "Stephen Hwang only"],
          ["23 Sep", "ORDER_HISTORY (snowflake)", "Finance Division", "Fiona Green, then Stephen after ownership was added"],
        ],
        [1000, 2700, 2100, 3560],
      ),
      p("", { after: 120 }),
      p("Two conclusions are visible in that table. Requests after the change carry a single domain-specific approver rather than a fixed trio, and the hardcoded data.steward account no longer appears."),

      h("8. Limitations and things to watch", HeadingLevel.HEADING_1),
      ...bullets([
        "A dataset with no domain, or a domain with no Data Steward, resolves to no approver at all. Seven domain-assigned datasets and 470 domainless datasets are currently in that position. Avoid demonstrating on those.",
        "Adding a second steward to a domain widens the audience for that domain. After the demo above, Finance Division has two stewards, so repeating Step 1 will no longer exclude you until one is removed.",
        "The corp group urn:li:corpGroup:6bb71640-29ce-4bd3-932c-9846458008af (display name “Data Stewards”) does not exist as an entity — entityExists returns false. It retains membership edges, so it appears in some views, but it cannot be used as an owner and DataHub rejects the attempt. Any workflow assigning it should be reviewed.",
        "The Dataset Access Request form can also be launched from Home rather than a dataset profile. Domain resolution needs a launching entity. All 23 historical requests carried one, so no failure was observed, but the path has not been explicitly tested.",
        "Definitions exported by fetch_action_workflows.py cannot be re-submitted unmodified: the server derives filterCondition (v2) from condition (v1), returns both, and then rejects input containing both. The derived value must be omitted.",
      ]),

      h("9. Reference", HeadingLevel.HEADING_1),
      table(
        ["Item", "Value"],
        [
          ["Workflow", { t: "urn:li:actionWorkflow:7110b19c-45b9-4450-b8bb-ba47d9f522ba", mono: true }],
          ["Definition in repo", { t: "workflows/definitions/action-workflow-dataset-access-request_v2.json", mono: true }],
          ["Validation helper", { t: "workflows/definitions/show_request_assignees.py", mono: true }],
          ["Deploy command", { t: "upsert_workflows.py --server $DATAHUB_GMS_URL --token-env DATAHUB_GMS_TOKEN --file <definition>", mono: true }],
          ["Ownership type used", { t: "urn:li:ownershipType:__system__data_steward", mono: true }],
        ],
        [2400, 6960],
      ),
    ],
  }],
});

Packer.toBuffer(doc).then((b) => {
  fs.writeFileSync("/tmp/docxbuild/domain-scoped-approvers.docx", b);
  console.log("written:", b.length, "bytes");
});
