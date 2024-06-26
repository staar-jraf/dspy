import re
from collections import namedtuple
from typing import Any, Union

import dsp
from dsp.primitives.demonstrate import Example

from .utils import format_answers, passages2text

Field = namedtuple("Field", "name separator input_variable output_variable description input")

# TODO: de-duplicate with dsp/templates/template.py


class TemplateV2:
    def __init__(
        self,
        template,
        format_handlers={
            "passages": passages2text,
            "context": passages2text,
            "answer": format_answers,
            "answers": format_answers,
        },
    ):
        self.format_handlers = format_handlers

        template = template.strip()

        self.instructions = re.search("(.*)\n", template).group(1)
        template = template[len(self.instructions) :].strip()

        self.fields = []
        while len(template) > 0:
            match = re.search("(.*)(\s){(.*)}\s(.*\${.*})", template)
            if match is not None:
                name = match.group(1)
                separator = match.group(2)
                variable = match.group(3)
                description = match.group(4)
            else:
                match = re.search("(.*)(\s){(.*)}", template)
                if match is not None:
                    name = match.group(1)
                    separator = match.group(2)
                    variable = match.group(3)
                    description = None
                else:
                    raise ValueError("Could not parse template")

            var_match = re.match("(.*) -> (.*)", variable)
            if var_match is not None:
                input_variable = var_match.group(1)
                output_variable = var_match.group(2)
            else:
                input_variable = variable
                output_variable = variable

            self.fields.append(
                Field(
                    name=name,
                    separator=separator,
                    input_variable=input_variable,
                    output_variable=output_variable,
                    description=description,
                ),
            )

            template = template[len(match.group(0)) :].strip()

    def query(self, example: Example, is_demo: bool = False) -> list[dict[str, Any]]:
        """Retrieves the input variables from the example and formats them into a query string."""
        result: list[dict[str, Any]] = []

        # If not a demo, find the last field that doesn't have a value set in `example` and set it to ""
        # This creates the "Output:" prefix at the end of the prompt.
        if not is_demo:
            has_value = [
                field.input_variable in example
                and example[field.input_variable] is not None
                and example[field.input_variable] != ""
                for field in self.fields
            ]

            # If there are no inputs, set the first field to ""
            if not any(has_value):
                example[self.fields[0].input_variable] = ""
            # Otherwise find the first field without a value.
            else:
                for i in range(1, len(has_value)):
                    if has_value[i - 1] and not any(has_value[i:]):
                        example[self.fields[i].input_variable] = ""
                        break

        result_: list[dict[str, Any]] = []
        for field in self.fields:
            if field.input_variable in example and example[field.input_variable] is not None:
                if field.input_variable in self.format_handlers:
                    format_handler = self.format_handlers[field.input_variable]
                else:

                    def format_handler(x):
                        assert type(x) == str, f"Need format_handler for {field.input_variable} of type {type(x)}"
                        return " ".join(x.split())

                formatted_value = format_handler(example[field.input_variable])
                separator = "\n" if field.separator == " " and "\n" in formatted_value else field.separator

                result_.append({
                    "role": "user" if field.input or not is_demo else "assistant",
                    "content": f"{field.name}{separator}{formatted_value}"
                })

        user_content = "\n\n".join([r["content"] for r in result_ if r["role"] == "user"])
        assistant_content = "\n\n".join([r["content"] for r in result_ if r["role"] == "assistant"])

        if user_content:
            result.append({"role": "user", "content": user_content})
        if assistant_content:
            result.append({"role": "assistant", "content": assistant_content})
        return result

    def guidelines(self, show_guidelines=True) -> str:
        """Returns the task guidelines as described in the lm prompt"""
        if (not show_guidelines) or (hasattr(dsp.settings, "show_guidelines") and not dsp.settings.show_guidelines):
            return ""

        result = "Follow the following format.\n\n"

        example = dsp.Example()
        for field in self.fields:
            example[field.input_variable] = field.description
        example.augmented = self._has_augmented_guidelines()

        result += "\n\n".join([r["content"] for r in self.query(example)])
        return result

    def _has_augmented_guidelines(self):
        return len(self.fields) > 3 or any(
            ("\n" in field.separator) or ("\n" in field.description) for field in self.fields
        )

    def extract(
        self,
        example: Union[Example, dict[str, Any]],
        raw_pred: str,
    ) -> Example:
        """Extracts the answer from the LM raw prediction using the template structure

        Args:
            example (Union[Example, dict[str, Any]]): Contains the input variables that raw_pred was completed on.
            raw_pred (str): LM generated string

        Returns:
            Example: The example with the output variables filled in
        """
        example = dsp.Example(example)

        raw_pred = raw_pred.strip()

        idx = 0
        while idx < len(self.fields):
            if self.fields[idx].input_variable not in example or example[self.fields[idx].input_variable] is None:
                break
            idx += 1

        import dspy

        idx = min(idx, len(self.fields) - 1)
        while raw_pred != "" and idx < len(self.fields):
            if idx < len(self.fields) - 1:
                next_field_name = "\n" + self.fields[idx + 1].name
                offset = raw_pred.find(next_field_name)

                if offset >= 0:
                    if dspy.settings.release >= 20231003:
                        example[self.fields[idx].output_variable] = raw_pred[:offset].strip().rstrip("---").strip()
                        raw_pred = raw_pred[offset + len(next_field_name) :].strip().rstrip("---").strip()
                    else:
                        example[self.fields[idx].output_variable] = raw_pred[:offset].strip()
                        raw_pred = raw_pred[offset + len(next_field_name) :].strip()

                    idx += 1
                else:
                    if dspy.settings.release >= 20231003:
                        example[self.fields[idx].output_variable] = raw_pred.strip().rstrip("---").strip()
                    else:
                        example[self.fields[idx].output_variable] = raw_pred.strip()

                    raw_pred = ""
                    idx += 1
                    break

            else:
                assert idx == len(self.fields) - 1, (idx, len(self.fields))

                if dspy.settings.release >= 20231003:
                    example[self.fields[idx].output_variable] = raw_pred.strip().rstrip("---").strip()
                else:
                    example[self.fields[idx].output_variable] = raw_pred.strip()

                break

        return example

    def __call__(self, example, show_guidelines=True) -> list[dict[str, Any]]:
        example = dsp.Example(example)

        if hasattr(dsp.settings, "query_only") and dsp.settings.query_only:
            return self.query(example)

        # The training data should not contain the output variable
        if self.fields[-1].input_variable in example:
            del example[self.fields[-1].input_variable]

        rdemos = [
            self.query(demo, is_demo=True)
            for demo in example.demos
            if (
                (not demo.get("augmented", False))
                and (  # validate that the training example has the same primitive input var as the template
                    self.fields[-1].input_variable in demo and demo[self.fields[-1].input_variable] is not None
                )
            )
        ]

        ademos = [self.query(demo, is_demo=True) for demo in example.demos if demo.get("augmented", False)]

        # Move the rdemos to ademos if rdemo has all the fields filled in
        rdemos_ = []
        new_ademos = []
        for rdemo in rdemos:
            rdemo_content = "\n\n".join([r["content"] for r in rdemo])
            if all((field.name in rdemo_content) for field in self.fields if field.input_variable in example):
                import dspy

                if dspy.settings.release >= 20230928:
                    new_ademos.append(rdemo)
                else:
                    ademos.append(rdemo)
            else:
                rdemos_.append(rdemo)

        new_ademos.extend(ademos)
        ademos = new_ademos
        rdemos = rdemos_

        ademos = [item for sublist in ademos for item in sublist]
        rdemos = [item for sublist in rdemos for item in sublist]

        long_query = self._has_augmented_guidelines()

        if long_query:
            example["augmented"] = True

        query = self.query(example)

        # if it has more lines than fields
        if len(query) > len(self.fields):
            long_query = True

            if not example.get("augmented", False):
                example["augmented"] = True
                query = self.query(example)


        if len(rdemos) >= 1 and len(ademos) == 0 and not long_query:
            parts = [
                self.instructions,
                self.guidelines(show_guidelines),
            ]
            user_content = "\n\n---\n\n".join([p.strip() for p in parts if p])
        elif len(rdemos) == 0:
            parts = [
                self.instructions,
                self.guidelines(show_guidelines),
            ]
            user_content = "\n\n---\n\n".join([p.strip() for p in parts if p])
        else:
            rdemos = "\n\n".join([r["content"] for r in rdemos])
            parts = [
                self.instructions,
                rdemos,
                self.guidelines(show_guidelines),
            ]
            user_content = "\n\n---\n\n".join([p.strip() for p in parts if p])

        return [{"role": "user", "content": user_content}, {"role": "assistant", "content": "OK, I'm ready."}, *ademos, *query]
