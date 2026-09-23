/**
 * Super nice tool for working with unicode ranges
 * @see https://www.zachleat.com/unicode-range-interchange/
 */
import Characterset from 'characterset';
import React from 'react';
import './index.css';

enum FontStyle {
  Normal = 'normal',
  Oblique = 'oblique',
}
interface Args {
  fontStyle: FontStyle;
  fontWeight: number;
}

const fontStyles = [FontStyle.Normal, FontStyle.Oblique];
const fontWeights = [100, 200, 300, 400, 500, 700, 900];

const unicodeRange =
  'U+0-24F,U+2C6,U+2DA,U+2DC,U+370-4FF,U+1E00-1EFF,U+2000-20CF,U+2150-215F,U+2212,U+2215,U+E0FF,U+EFFD,U+F000';
const characterSet = Characterset.parseUnicodeRange(unicodeRange);
const characterSetStr = String.fromCodePoint(...characterSet.toArray());

export default {
  title: 'Foundations/Typography/Font Faces',
  parameters: {
    docs: {
      source: {
        code: null,
      },
    },
  },
};

const Template = (args: Args) => {
  const style = { ...args, fontFamily: 'Inter' };
  return (
    <span style={style} className="atm-break-all">
      {characterSetStr}
    </span>
  );
};

/**
 * Generators are the best way to dynamically create stories.
 * However, you still need to know the order in which stories are yielded.
 *
 * This will first create stories for "normal" and each font weight
 *  followed by "oblique" and each font weight.
 *
 * @param fontStyles
 * @param fontWeights
 */
function* generateFontFaceStories(
  fontStyles: FontStyle[],
  fontWeights: number[]
) {
  for (const fontStyle of fontStyles) {
    for (const fontWeight of fontWeights) {
      const Story = Template.bind({});
      Story.args = { fontStyle, fontWeight };
      yield Story;
    }
  }
}

const stories = generateFontFaceStories(fontStyles, fontWeights);
