# The demo page

`GET /` shows an interactive page. It uses plain HTML, CSS, and JavaScript, with no libraries.
The page reads only the documented response. Thus it is also a test of the response schema.

What the user can do:

- **Ask** a question, or select one of nine examples.
- **Read** the title, one sentence about the chart, the number of trials, and the chart.
- **Point** to a mark to see exact numbers. **Change** to a table with one click.
- **Zoom** scatter plots and networks. Zoom moves marks apart and does not make them larger.
- **Click** a bar, point, node, or line to see its trials and their registry values.
- **Set filters** with controls. The active filters show as labels that the user can remove.

When there is no chart:

- The cause shows **at the top of the page**.
- A maximum of three **one-click suggestions** show below the cause, with trial counts.
- The question box is selected, so the user can type a new question immediately.

The AI assistant tested the page in Chrome with more than 50 questions: all chart types,
filters, misspelled names, another language, HTML in the question, questions with no answer,
zero results, and too many results. The tests found these defects, and each one is corrected:

| Defect found | Correction |
|---|---|
| Some suggested questions failed when the user selected them. | The service examines each suggestion before it offers it. |
| Zero trials showed an empty chart with no next step. | The page shows the cause at the top, with wider questions and their counts. |
| The trial list showed registry codes such as `NOT_YET_RECRUITING`. | The list shows plain labels. The code shows with the record path. |
| The sentence for one trial said "1 trial are shown". | Corrected. |
| The sentence for a yes/no chart said "No has the most". | It now gives the share, for example "375 of 1,066 trials (35%)". |

Speed measured in these tests:

- Most answers: 3 to 8 s. The planner takes 2 to 4 s of this time.
- First question about a large subject (10,000 to 20,000 trials): 13 to 28 s.

---

Back to the [README](../README.md).
